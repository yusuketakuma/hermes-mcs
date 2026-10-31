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


def test_idle_runs_do_not_hide_matching_failure_evidence(data):
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload)
    before = _read(ledger, job, cfg)
    ledger.db.executemany("INSERT INTO artifacts(kind,content) VALUES('semantic_drain_run',?)",
                          [(json.dumps({'job_metrics': []}),)] * 513)
    ledger.db.commit()
    assert _read(ledger, job, cfg)['reason_code'] == 'budget_short'
    ledger.db.execute('BEGIN')
    try:
        report = diag.list_failed(ledger.db, cfg)
    finally:
        ledger.db.rollback()
    assert report['jobs'][0]['reason_code'] == 'budget_short'
    assert report['jobs'][0]['diagnosis_hash'] == before['diagnosis_hash']


def test_one_jobs_history_does_not_hide_another_jobs_evidence(data):
    ledger, job, payload, _ = data
    other = job + 100
    _metric(ledger, other, payload)
    metric = {"job_id": job, "project_id": 1, "kind": "semantic", "generation": payload["generation"],
              "payload_digest": payload_hash(payload), "attempts": 2, "status": "retry",
              "retry_reason": "budget_short"}
    content = json.dumps({"job_metrics": [metric]})
    ledger.db.executemany("INSERT INTO artifacts(kind,content) VALUES('semantic_drain_run',?)", [(content,)] * 513)
    ledger.db.commit()
    ledger.db.execute('BEGIN')
    try:
        metrics = diag._metrics(ledger.db, [job, other])
    finally:
        ledger.db.rollback()
    assert sum(item[1] == job for item in metrics) == 512
    assert any(item[1] == other and item[-1] == 'budget_short' for item in metrics)


def test_sqlite_opaque_newer_json_is_strictly_parsed(data):
    import sys
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload)
    metric = {"job_id": job, "project_id": 1, "kind": "semantic", "generation": payload["generation"],
              "payload_digest": payload_hash(payload), "attempts": 2, "status": "retry",
              "retry_reason": None, "llm_error": None}
    raw = '{"job_metrics":[' + json.dumps(metric) + '],"nested":' + '[' * 2100 + '0' + ']' * 2100 + '}'
    assert ledger.db.execute('SELECT json_valid(?)', (raw,)).fetchone()[0] == 0
    ledger.artifact_add('semantic_drain_run', raw)
    ledger.db.commit()
    previous_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(8000)
        assert _read(ledger, job, cfg)['reason_code'] == 'unknown'
    finally:
        sys.setrecursionlimit(previous_limit)


def test_newest_bound_metric_without_reason_remains_unknown(data):
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload)
    _metric(ledger, job, payload, retry_reason=None, llm_error=None)
    assert _read(ledger, job, cfg)['reason_code'] == 'unknown'


def test_candidate_json_never_confuses_boolean_id_or_duplicate_keys(data):
    ledger, job, payload, cfg = data
    metric = {'job_id': True, 'project_id': 1, 'kind': 'semantic', 'generation': payload['generation'],
              'payload_digest': payload_hash(payload), 'attempts': 2, 'status': 'retry', 'retry_reason': 'budget_short'}
    ledger.artifact_add('semantic_drain_run', json.dumps({'job_metrics': [metric]}))
    ledger.artifact_add('semantic_drain_run', '{"job_metrics":[],"job_metrics":[' + json.dumps(metric) + ']}')
    ledger.db.commit()
    assert _read(ledger, job, cfg)['reason_code'] == 'unknown'


def test_metric_time_limit_returns_unknown_without_changing_data(data, monkeypatch):
    from types import SimpleNamespace
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload)
    clock = iter((0.0, 5.0))
    monkeypatch.setattr(diag, 'time', SimpleNamespace(monotonic=lambda: next(clock)))
    before = ledger.db.total_changes
    assert _read(ledger, job, cfg)['reason_code'] == 'unknown'
    assert ledger.db.total_changes == before


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
    assert result["jobs"][0]["reason_code"] == "unknown"
    assert "reason_unverified" in result["jobs"][0]["warnings"]


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


@pytest.mark.parametrize('code,remedy', [
    ('audit_claims_empty', 'quality'), ('audit_claims_invalid', 'quality'),
    ('audit_client_unavailable', 'jev'), ('audit_options_invalid', 'investigate'),
    ('audit_response_invalid', 'jev'), ('audit_jev_None', 'investigate'),
    ('audit_unevaluated', 'investigate'),
])
def test_bound_audit_failure_codes_are_reported_without_guessing(data, code, remedy):
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload, retry_reason=code)
    report = _read(ledger, job, cfg)
    assert report['reason_code'] == code and report['remediation_code'] == remedy
    assert report['reason_basis'] == 'bound_job_metric'


def test_recent_bound_evidence_does_not_sort_large_old_history(data):
    ledger, job, payload, cfg = data
    idle = json.dumps({'job_metrics': []})
    ledger.db.executemany("INSERT INTO artifacts(kind,content) VALUES('semantic_drain_run',?)", [(idle,)] * 30000)
    _metric(ledger, job, payload)
    steps = [0]

    def stop_long_scan():
        steps[0] += 1
        return steps[0] > 100

    ledger.db.set_progress_handler(stop_long_scan, 1000)
    try:
        assert _read(ledger, job, cfg)['reason_code'] == 'budget_short'
    finally:
        ledger.db.set_progress_handler(None, 0)


def test_interrupted_prefix_keeps_observed_job_and_leaves_other_unknown(data, monkeypatch):
    from types import SimpleNamespace
    ledger, job, payload, cfg = data
    ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,body_state) VALUES(43,1,'synthetic','full')")
    other_payload = {**payload, 'targets': [43]}
    other = ledger.job_add('semantic', 1, 43, payload=other_payload)
    ledger.db.execute("UPDATE fetch_jobs SET state='failed',attempts=3 WHERE job_id=?", (other,))
    ledger.artifact_add('semantic_drain_run', json.dumps({'job_metrics': []}))
    _metric(ledger, job, payload)
    clock = [0.0]
    original = diag.strict_json

    def parse_and_stop(raw):
        result = original(raw)
        if isinstance(result, dict) and result.get('job_metrics'):
            clock[0] = 5.0
        return result

    monkeypatch.setattr(diag, 'strict_json', parse_and_stop)
    monkeypatch.setattr(diag, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    before = ledger.db.total_changes
    ledger.db.execute('BEGIN')
    try:
        reports = {entry['job_id']: entry for entry in diag.list_failed(ledger.db, cfg, now=1700000000)['jobs']}
    finally:
        ledger.db.rollback()
    assert reports[job]['reason_code'] == 'budget_short'
    assert reports[other]['reason_code'] == 'unknown'
    assert 'reason_unverified' in reports[other]['warnings']
    assert ledger.db.total_changes == before


@pytest.mark.parametrize('bad', ['oversized', 'too_many', 'duplicate', 'invalid', 'nontext', 'missing'])
def test_unreadable_newer_run_never_reveals_older_reason(data, bad):
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload)
    reasonless = {"job_id": job, "project_id": 1, "kind": "semantic", "generation": payload["generation"],
                  "payload_digest": payload_hash(payload), "attempts": 2, "status": "retry"}
    content = {
        'oversized': json.dumps({'job_metrics': [reasonless], 'padding': 'x' * 1048576}),
        'too_many': json.dumps({'job_metrics': [reasonless] + [{}] * 64}),
        'duplicate': '{"job_metrics":[],"job_metrics":[' + json.dumps(reasonless) + ']}',
        'invalid': '{"job_metrics":',
        'nontext': b'42',
        'missing': '{}',
    }[bad]
    ledger.db.execute("INSERT INTO artifacts(kind,content) VALUES('semantic_drain_run',?)", (content,))
    ledger.db.commit()
    before = ledger.db.total_changes
    report = _read(ledger, job, cfg)
    assert report['reason_code'] == 'unknown'
    assert 'reason_unverified' in report['warnings']
    assert ledger.db.total_changes == before


def test_newer_reasonless_bound_job_and_other_job_survive_unreadable_older_run(data):
    ledger, job, payload, cfg = data
    ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,body_state) VALUES(43,1,'synthetic','full')")
    other_payload = {**payload, 'targets': [43]}
    other = ledger.job_add('semantic', 1, 43, payload=other_payload)
    ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,body_state) VALUES(44,1,'synthetic','full')")
    unresolved = ledger.job_add('semantic', 1, 44, payload={**payload, 'targets': [44]})
    ledger.db.execute("UPDATE fetch_jobs SET state='failed',attempts=3 WHERE job_id IN (?,?)", (other, unresolved))
    _metric(ledger, job, payload)
    invalid_id = ledger.artifact_add('semantic_drain_run', '{"job_metrics":')
    _metric(ledger, other, other_payload)
    _metric(ledger, job, payload, retry_reason=None, llm_error=None)
    queries = []
    ledger.db.set_trace_callback(queries.append)
    ledger.db.execute('BEGIN')
    try:
        reports = {entry['job_id']: entry for entry in diag.list_failed(ledger.db, cfg, now=1700000000)['jobs']}
    finally:
        ledger.db.rollback()
        ledger.db.set_trace_callback(None)
    assert any(f'FROM main.artifacts WHERE artifact_id={invalid_id}' in query for query in queries)
    assert reports[job]['reason_code'] == 'unknown'
    assert 'reason_unverified' in reports[job]['warnings']
    assert reports[other]['reason_code'] == 'budget_short'
    assert reports[unresolved]['reason_code'] == 'unknown'
    assert 'reason_unverified' in reports[unresolved]['warnings']


def test_binding_preparation_stops_when_budget_expires(data, monkeypatch):
    from types import SimpleNamespace
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload)
    clock = [0.0]
    original = diag.retry_job_row

    def delayed_binding(*args):
        result = original(*args)
        clock[0] = 5.0
        return result

    monkeypatch.setattr(diag, 'retry_job_row', delayed_binding)
    monkeypatch.setattr(diag, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    queries = []
    ledger.db.set_trace_callback(queries.append)
    try:
        assert _read(ledger, job, cfg)['reason_code'] == 'unknown'
    finally:
        ledger.db.set_trace_callback(None)
    assert not any('SELECT artifact_id, kind FROM main.artifacts' in query for query in queries)


@pytest.mark.parametrize('field,value', [
    ('attempts', None), ('payload_digest', None), ('project_id', True),
    ('kind', None), ('generation', []), ('status', 1),
    ('payload_digest', ''), ('payload_digest', 'not-a-digest'),
    ('payload_digest', '0' * 63), ('payload_digest', 'g' * 64), ('payload_digest', 'A' * 64),
])
@pytest.mark.parametrize('same_artifact', [False, True])
def test_newer_invalid_job_binding_does_not_reveal_older_reason(data, field, value, same_artifact):
    ledger, job, payload, cfg = data
    metric = {"job_id": job, "project_id": 1, "kind": "semantic", "generation": payload["generation"],
              "payload_digest": payload_hash(payload), "attempts": 2, "status": "retry",
              "retry_reason": "budget_short"}
    newer = {**metric, field: value}
    if same_artifact:
        ledger.artifact_add('semantic_drain_run', json.dumps({'job_metrics': [metric, newer]}))
    else:
        _metric(ledger, job, payload)
        ledger.artifact_add('semantic_drain_run', json.dumps({'job_metrics': [newer]}))
    report = _read(ledger, job, cfg)
    assert report['reason_code'] == 'unknown'
    assert 'reason_unverified' in report['warnings']


def test_valid_other_binding_does_not_hide_older_bound_reason(data):
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload)
    _metric(ledger, job, payload, payload_digest='0' * 64)
    assert _read(ledger, job, cfg)['reason_code'] == 'budget_short'


def test_invalid_binding_only_stops_its_job(data):
    ledger, job, payload, cfg = data
    ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,body_state) VALUES(43,1,'synthetic','full')")
    other_payload = {**payload, 'targets': [43]}
    other = ledger.job_add('semantic', 1, 43, payload=other_payload)
    ledger.db.execute("UPDATE fetch_jobs SET state='failed',attempts=3 WHERE job_id=?", (other,))
    _metric(ledger, job, payload)
    _metric(ledger, other, other_payload)
    _metric(ledger, job, payload, attempts=None)
    ledger.db.execute('BEGIN')
    try:
        reports = {entry['job_id']: entry for entry in diag.list_failed(ledger.db, cfg, now=1700000000)['jobs']}
    finally:
        ledger.db.rollback()
    assert reports[job]['reason_code'] == 'unknown'
    assert reports[other]['reason_code'] == 'budget_short'


def test_bound_newest_metric_precedes_invalid_older_metric_in_same_run(data):
    ledger, job, payload, cfg = data
    metric = {"job_id": job, "project_id": 1, "kind": "semantic", "generation": payload["generation"],
              "payload_digest": payload_hash(payload), "attempts": 2, "status": "retry",
              "retry_reason": "budget_short"}
    ledger.artifact_add('semantic_drain_run', json.dumps({'job_metrics': [{**metric, 'attempts': None}, metric]}))
    assert _read(ledger, job, cfg)['reason_code'] == 'budget_short'


@pytest.mark.parametrize('ambiguous', [
    None, 1, {}, {'job_id': None}, {'job_id': True}, {'job_id': '1'},
    {'job_id': 0}, {'job_id': -1}, {'job_id': 2**63},
])
def test_unidentifiable_metric_preserves_newer_binding_and_blocks_older(data, ambiguous):
    ledger, job, payload, cfg = data
    ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,body_state) VALUES(43,1,'synthetic','full')")
    other_payload = {**payload, 'targets': [43]}
    other = ledger.job_add('semantic', 1, 43, payload=other_payload)
    ledger.db.execute("UPDATE fetch_jobs SET state='failed',attempts=3 WHERE job_id=?", (other,))
    metric = {"job_id": job, "project_id": 1, "kind": "semantic", "generation": payload["generation"],
              "payload_digest": payload_hash(payload), "attempts": 2, "status": "retry",
              "retry_reason": "budget_short"}
    newer = {**metric, 'job_id': other, 'payload_digest': payload_hash(other_payload)}
    ledger.artifact_add('semantic_drain_run', json.dumps({'job_metrics': [metric, ambiguous, newer]}))
    ledger.db.execute('BEGIN')
    try:
        reports = {entry['job_id']: entry for entry in diag.list_failed(ledger.db, cfg, now=1700000000)['jobs']}
    finally:
        ledger.db.rollback()
    assert reports[job]['reason_code'] == 'unknown'
    assert 'reason_unverified' in reports[job]['warnings']
    assert reports[other]['reason_code'] == 'budget_short'


def test_idle_and_other_job_metric_do_not_hide_bound_evidence(data):
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload)
    ledger.artifact_add('semantic_drain_run', json.dumps({'job_metrics': []}))
    ledger.artifact_add('semantic_drain_run', json.dumps({'job_metrics': [{'job_id': job + 100}]}))
    assert _read(ledger, job, cfg)['reason_code'] == 'budget_short'
