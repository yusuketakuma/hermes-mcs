"""Synthetic cloud context execution and publication contracts."""
import extract_llm_context
import extract_llm_errors
import extract_llm_prepare
import extract_llm_progress
import extract_llm_proofs
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_selection
import extract_llm_store

import patient_context_quality_controller
import patient_context_refresh_readiness
import patient_context_refresh_sources
import patient_context_refresh_state
import patient_context_plan as context_plan
import json
import os
import time
import pytest
import extract_llm as worker
import cloud_inference
import patient_context_refresh as context_refresh
from extract_testkit import _message
from test_patient_context_refresh import _semantic_proof, document, seed, stamp
from anthropic_context_testkit import (
    _SOURCE_REPAIR_PROVIDER as _SOURCE_REPAIR_PROVIDER,
    _SOURCE_REPAIR_JEV as _SOURCE_REPAIR_JEV,
    CFG, cloud, drain, runtime, source_repair, step
)

__all__ = ["runtime", "cloud", "source_repair"]


def test_patient_context_accepts_only_known_current_or_historical_cloud_contracts():
    for model, effort in worker.openai_client.VALID_MODEL_EFFORTS:
        contract = cloud_inference.cloud_provider_contract(selection={"provider": "openai", "model": model, "effort": effort})
        assert patient_context_refresh_readiness._saved_cloud_contract(contract) == contract
        assert patient_context_refresh_readiness._saved_cloud_contract({**contract, "prompt": "0" * 16}) is None
        assert patient_context_refresh_readiness._saved_cloud_contract({**contract, "model": "synthetic-unknown"}) is None


def test_patient_request_precheck_uses_selected_repair_limit_and_never_sends_when_exhausted(runtime, monkeypatch):
    ledger, clock, _ = runtime
    cfg = {"cloud_llm": {"mode": "on", "provider": "openai", "project_ids": [1]}}
    selection, errors = worker.cloud_policy.resolve_profile(
        cfg, route="patient_context", purpose="quality_repair", root="/synthetic")
    assert not errors
    seen = []
    def blocked(_ledger, _cfg, **kwargs):
        seen.append(kwargs)
        return "monthly_cap"
    monkeypatch.setattr(worker.cloud_policy, "budget_block_reason", blocked)
    clock.mono = time.monotonic()
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(worker.cloud_policy, 'selected_key', lambda *a: 'synthetic-key')
    monkeypatch.setattr(worker.cloud_policy, 'pricing_valid', lambda *a, **kw: True)
    monkeypatch.setattr(cloud_inference.writer_lock, 'updater_state', lambda: (False, 1))
    provider = cloud_inference.CloudInferenceUnit(ledger, cfg, route='patient_context',
        purpose='quality_repair', selection=selection, project_id=1, unit_key='synthetic-repair',
        deadline=clock.mono + 1000, current=lambda: True, dispatch=lambda fn, **kw: fn())
    monkeypatch.setattr(provider.client, 'complete', lambda *a, **kw: pytest.fail('no fresh send'))
    with pytest.raises(cloud_inference.CloudFallback, match='cloud_send_blocked'):
        patient_context_quality_controller._ask(cfg, {'synthetic': 'request'}, clock.mono + 1000,
                             lambda: True, provider=provider)
    assert not ledger.artifacts('cloud_usage_reserve')
    assert seen[0]["max_tokens"] == 32768 and seen[0]["selection"] == selection
    assert seen[0]["input_bound"] > 0


def test_daytime_cloud_needs_no_local_gpu_capacity(runtime, cloud):
    led, clock, local_calls = runtime
    clock.wall = stamp("2026-10-18T12:00:00")
    seed(led, body="見出しのない完全合成の居住記録。")
    assert led.db.execute("SELECT first_seen FROM messages WHERE message_id=1").fetchone()[0] <= clock.wall
    drain(runtime, local_admit=lambda: False)
    assert cloud[0] and not local_calls
    import semantic_runtime
    usage = led.artifacts('semantic_usage', project_id=1)
    assert led.synthetic_jev_requests and len(usage) == len(led.synthetic_jev_requests)
    assert all(semantic_runtime.patient_summary_usage_resolved(led.db, 1, json.loads(row['meta'])) for row in usage)
    assert led.artifacts(patient_context_refresh_state.QUALITY, project_id=1)
    assert led.artifacts(patient_context_refresh_state.QUALITY_PASS, project_id=1)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    scan = led.db.execute("SELECT meta FROM artifacts WHERE kind=?", (patient_context_refresh_state.SCAN,)).fetchone()
    meta = json.loads(scan[0])
    assert meta["provider"] == cloud_inference.cloud_provider_contract()
    assert meta["model"] == "claude-haiku-5-5"
    assert meta["contract"] == patient_context_refresh_state._contract(CFG, 1)


def test_archived_cloud_source_is_audited_but_never_published(runtime, cloud):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    with led.db:
        led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    for _ in range(20):
        result = step(runtime)
        if result.get("lane") == "C" and result["status"] == "progressed":
            break
    else:
        pytest.fail("archived source audit did not finish")
    assert led.artifacts("patient_context_source_quality_v1", project_id=1)
    assert led.artifacts("patient_context_source_quality_pass_v1", project_id=1)
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG, hydrate=False)
    scan = patient_context_refresh_sources._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"], cfg=CFG)[1]
    assert scan["complete"] and scan["audited"]
    assert not patient_context_refresh_readiness._inference_coverage(led.db, 1, CFG, snapshot)["publication_eligible"]
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
    led.db.execute("BEGIN")
    try:
        with pytest.raises(ValueError, match="patient_summary_quality_source_incomplete"):
            patient_context_refresh_sources._quality_capture(led.db, 1, CFG, "synthetic", [],
                                             {"code": "a" * 64, "policy": "b" * 64}, {"groups": {}})
    finally:
        led.db.rollback()
    before = len(led.synthetic_jev_requests)
    with led.db:
        led.db.execute("UPDATE patients SET is_archived=0 WHERE project_id=1")
    active = patient_context_refresh_sources._snapshot(led.db, 1, CFG, hydrate=False)
    changed = patient_context_refresh_sources._current_scans(led.db, 1, active["sources"], active["contract"], cfg=CFG)[1]
    assert not changed.get("audited")
    assert len(led.synthetic_jev_requests) == before


def test_requested_cloud_failure_falls_back_to_verified_local_context(runtime, cloud):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[2][0] = True
    result = step(runtime)
    assert result['status'] == 'progressed'
    for _ in range(30):
        result = step(runtime)
        if patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG):
            break
    else:
        pytest.fail('verified local fallback did not become usable')
    assert local_calls and not cloud[0]
    assert not patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']
    assert patient_context_refresh_readiness.status(led.db, CFG)['history_pending'] == 1
    count = len(local_calls)
    for _ in range(3):
        assert step(runtime)['status'] != 'complete'
    assert len(local_calls) == count and not cloud[0]


def test_failed_cloud_does_not_bypass_local_admission(runtime, cloud):
    led, _, local_calls = runtime
    seed(led)
    # An explicit source fact makes the first summary actually call its provider.
    assert patient_context_refresh_sources._snapshot(led.db, 1, CFG)["sources"][0]["items"]
    cloud[2][0] = True
    result = step(runtime, local_admit=lambda: False)
    assert result["status"] == "deferred"
    assert not local_calls and document(led) is None


def test_local_fallback_does_not_skip_a_preexisting_negative_quality_result(runtime, cloud, monkeypatch):
    import semantic_jev
    from inference_version import CURRENT_VERSION, VERSION_KEY

    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    failure_id = led.artifact_add(patient_context_refresh_state.QUALITY_FAIL, '{}', project_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, 'input_key': '1' * 64, 'candidate_key': '2' * 64,
              'failure_key': '3' * 64, 'audit_key': '4' * 64})
    cloud[2][0] = True
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *_: None)
    for _ in range(20):
        result = step(runtime)
        if result.get('reason') in ('source_quality_deferred', 'summary_quality_deferred'):
            break
    assert result.get('reason') in ('source_quality_deferred', 'summary_quality_deferred')
    assert local_calls and patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (failure_id,)).fetchone()


@pytest.mark.parametrize('failure_kind', ['summary', 'source'])
def test_local_mode_requires_new_quality_pass_after_prior_negative(runtime, cloud, monkeypatch, failure_kind):
    import semantic_jev
    from inference_version import CURRENT_VERSION, VERSION_KEY

    led, clock, local_calls = runtime
    seed(led, body="完全合成の居住記録です。")
    kind = (patient_context_refresh_state.QUALITY_FAIL if failure_kind == 'summary' else
            patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_FAIL, {'purpose': 'source'}))
    failure_id = led.artifact_add(kind, '{}', project_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, 'input_key': '1' * 64, 'candidate_key': '2' * 64,
              'failure_key': '3' * 64, 'audit_key': '4' * 64})
    cloud[1][0] = 'off'
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *_: None)
    for _ in range(20):
        result = step(runtime)
        if result.get('reason') in ('source_quality_deferred', 'summary_quality_deferred'):
            break
    assert result.get('reason') in ('source_quality_deferred', 'summary_quality_deferred')
    assert local_calls and patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (failure_id,)).fetchone()
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *_: 'synthetic-jev-key')
    clock.wall += 120
    trace = []
    for _ in range(30):
        outcome = step(runtime)
        trace.append(outcome)
        if outcome['status'] == 'complete':
            break
    assert outcome['status'] == 'complete', trace
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    assert led.artifacts(patient_context_refresh_state.QUALITY_PASS, project_id=1)
    if failure_kind == 'source':
        assert led.artifacts(patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_PASS, {'purpose': 'source'}), project_id=1)
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (failure_id,)).fetchone()
    with led.db:
        led.db.execute("UPDATE artifacts SET content='{}' WHERE kind=?", (patient_context_refresh_state.CHUNK,))
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None


def test_summary_negative_does_not_invalidate_source_skip(runtime, cloud, monkeypatch):
    import semantic_jev
    from inference_version import CURRENT_VERSION, VERSION_KEY

    led, _, _ = runtime
    seed(led, body='完全合成の居住記録です。')
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *_: None)
    source_skip = patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_SKIP, {'purpose': 'source'})
    for _ in range(20):
        step(runtime)
        if led.artifacts(source_skip):
            break
    assert led.artifacts(source_skip)
    meta = {VERSION_KEY: CURRENT_VERSION, 'input_key': '1' * 64, 'candidate_key': '2' * 64,
            'failure_key': '3' * 64, 'audit_key': '4' * 64}
    led.artifact_add(patient_context_refresh_state.QUALITY_FAIL, '{}', project_id=1, meta=meta)
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG)
    scans = patient_context_refresh_sources._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)
    assert scans[1]['_audit_skipped'] is True
    source_failure = patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_FAIL, {'purpose': 'source'})
    led.artifact_add(source_failure, '{}', project_id=1, meta=meta)
    scans = patient_context_refresh_sources._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)
    assert scans[1]['_audit_skipped'] is False


@pytest.mark.parametrize('mode', ['on', 'off'])
@pytest.mark.parametrize('purpose', ['source', 'summary'])
def test_mid_audit_budget_skip_cannot_override_prior_negative(runtime, cloud, monkeypatch, mode, purpose):
    import semantic_audit
    import semantic_jev
    from inference_version import CURRENT_VERSION, VERSION_KEY

    led, clock, _ = runtime
    seed(led, body='完全合成の居住記録です。')
    kind = patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_FAIL, {'purpose': purpose})
    led.artifact_add(kind, '{}', project_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, 'input_key': '1' * 64, 'candidate_key': '2' * 64,
              'failure_key': '3' * 64, 'audit_key': '4' * 64})
    cloud[1][0] = mode
    name = 'audit_patient_quality_page' if purpose == 'source' else 'audit_patient_summary_chunk'
    real = getattr(semantic_audit, name)
    def exhausted(client, *args, **kwargs):
        client.last_error = semantic_jev.JevError('budget_exceeded', 'daily_cap')
        return {'evaluated': False}
    monkeypatch.setattr(semantic_audit, name, exhausted)
    reason = purpose + '_quality_deferred'
    for _ in range(25):
        result = step(runtime)
        if result.get('reason') == reason:
            break
    assert result.get('reason') == reason
    assert not led.artifacts(patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_SKIP, {'purpose': purpose}))
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
    monkeypatch.setattr(semantic_audit, name, real)
    clock.wall += 120
    drain(runtime)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)


def test_shadow_runs_only_after_local_commit_and_never_publishes_cloud(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[1][0] = "shadow"
    original = cloud_inference.CloudInferenceUnit
    class Shadow(original):
        def __call__(self, prompt, **kwargs):
            assert not led.db.in_transaction
            payload = json.loads(prompt)
            kind = patient_context_refresh_state.KIND if "reports" in payload else patient_context_refresh_state.SCAN
            assert led.db.execute("SELECT 1 FROM artifacts WHERE kind=?", (kind,)).fetchone()
            return super().__call__(prompt, **kwargs)
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Shadow)
    drain(runtime)
    assert local_calls and cloud[0]
    assert led.artifacts("cloud_compare_v1")
    for row in led.db.execute("SELECT meta FROM artifacts WHERE kind IN (?,?)", (patient_context_refresh_state.KIND, patient_context_refresh_state.SCAN)):
        assert "provider" not in json.loads(row[0])


def test_crossing_midnight_preserves_cloud_publication_and_progress(runtime, cloud, monkeypatch):
    led, clock, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    clock.wall = stamp("2026-10-17T23:59:30")
    original = cloud_inference.CloudInferenceUnit
    class Crossing(original):
        def __call__(self, prompt, **kwargs):
            answer = super().__call__(prompt, **kwargs)
            # Cross once; later answers must not rewind the observed QC clock.
            clock.wall = max(clock.wall, stamp("2026-10-18T00:00:01"))
            clock.mono += 31
            return answer
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Crossing)
    # First cloud unit scans the original source before any summary is published.
    result = step(runtime)
    assert result["status"] == "progressed"
    assert led.artifacts(patient_context_refresh_state.SCAN)
    meta = led.db.execute("SELECT meta FROM artifacts WHERE kind=? AND project_id=1", (patient_context_refresh_state.SCAN,)).fetchone()
    assert json.loads(meta[0])["night_id"] == "2026-10-17"
    assert not led.synthetic_cloud_dispositions
    before = len(cloud[0])
    drain(runtime)
    after = len(cloud[0])
    assert after >= before
    assert step(runtime)["status"] == "complete" and len(cloud[0]) == after


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_cloud_completed_data_is_not_reused_after_route_change(runtime, cloud, mode):
    led, clock, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    drain(runtime)
    assert not local_calls
    cloud[1][0] = mode
    # the published context stays readable while its backend's request shape is
    # current; only the rebuild under the new route is due
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    snapshot, _, pending = patient_context_refresh_readiness._pending(led.db, 1, CFG)
    assert pending["A"] and pending["B"]
    assert not patient_context_refresh_sources._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])
    clock.wall += 3 * 60 * 60
    clock.mono += 3 * 60 * 60
    drain(runtime)
    assert local_calls and patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    current = patient_context_refresh_sources._snapshot(led.db, 1, CFG, hydrate=False)
    scans = patient_context_refresh_sources._current_scans(led.db, 1, current['sources'], current['contract'])
    assert scans and all(not scan.get('_provider_contract') for scan in scans.values())
    assert not patient_context_refresh_state._latest(led.db, patient_context_refresh_state.KIND, 1)[1].get('provider')
    assert any(json.loads(row[0]).get('provider') for row in led.db.execute(
        "SELECT meta FROM artifacts WHERE kind=?", (patient_context_refresh_state.SCAN,)))


def test_partial_cloud_scan_requires_same_rules_and_provider_contract(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led)
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG)
    source = snapshot["sources"][0]
    meta = {"source_sig": context_plan.source_signature(source), "contract": snapshot["contract"],
            "provider": {**snapshot["contract"]["provider"], "prompt": "0" * 16}}
    with led.db:
        patient_context_refresh_state._replace(led, patient_context_refresh_state.SCAN, 1, {"cursor": 1, "complete": False, "facts": []}, meta, 1)
    assert not patient_context_refresh_sources._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])
    meta["provider"] = snapshot["contract"]["provider"]
    with led.db:
        patient_context_refresh_state._replace(led, patient_context_refresh_state.SCAN, 1, {"cursor": 1, "complete": False, "facts": []}, meta, 1)
    assert patient_context_refresh_sources._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])[1]["cursor"] == 1
    # a code release that leaves the inference rules alone keeps the scan
    monkeypatch.setattr(worker, "_upgrade_contract", lambda: "f" * 64)
    monkeypatch.setattr(extract_llm_qc, '_upgrade_contract', worker._upgrade_contract)
    monkeypatch.setattr(extract_llm_store, '_upgrade_contract', worker._upgrade_contract)
    assert patient_context_refresh_sources._snapshot(led.db, 1, CFG)["contract"] == snapshot["contract"]
    # an inference rule version change rebuilds it
    monkeypatch.setattr(worker, "EXTRACT_VERSION", worker.EXTRACT_VERSION + 1)
    monkeypatch.setattr(extract_llm_context, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_errors, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_prepare, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_progress, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_proofs, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_provider, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_qc, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_resident, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_selection, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_store, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    changed, _, pending = patient_context_refresh_readiness._pending(led.db, 1, CFG)
    assert pending["B"] and changed["contract"] != snapshot["contract"]
    assert not patient_context_refresh_sources._current_scans(led.db, 1, changed["sources"], changed["contract"])


def test_scope_is_rechecked_after_writer_acquisition(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    # The first selected cloud scan must be revoked after its HTTP response.
    assert not cloud[0] and not led.synthetic_cloud_dispositions
    original = cloud_inference.CloudInferenceUnit
    checked = []
    class RevokedAtCommit(original):
        def guard(self):
            checked.append(led.db.in_transaction)
            if led.db.in_transaction:
                raise cloud_inference.CloudFallback("synthetic_changed_while_waiting")
            return super().guard()
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", RevokedAtCommit)
    result = step(runtime)
    assert result["status"] == "deferred" and len(cloud[0]) == 1
    assert False in checked and True in checked
    assert not led.artifacts(patient_context_refresh_state.SCAN)
    assert led.synthetic_cloud_dispositions == ["discarded_scope"]


@pytest.mark.parametrize("mutation", ["actual_cloud_without_provider", "local_model", "route_model", "actual_model"])
def test_fallback_reuse_requires_exact_local_and_route_contract(runtime, cloud, mutation):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG)
    contract = snapshot['contract']
    # A previously saved local fallback is read-only compatibility evidence;
    # current explicit cloud work no longer creates one on provider failure.
    meta = {'source_sig': context_plan.source_signature(snapshot['sources'][0]),
            'contract': {key: value for key, value in contract.items() if key != 'provider'},
            'route_contract': contract, 'model': 'synthetic'}
    with led.db:
        patient_context_refresh_state._replace(led, patient_context_refresh_state.SCAN, 1,
                                 {'cursor': 1, 'complete': False, 'facts': []}, meta, 1)
    assert 1 in patient_context_refresh_sources._current_scans(led.db, 1, snapshot['sources'], contract)
    row = led.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind=?", (patient_context_refresh_state.SCAN,)).fetchone()
    meta = json.loads(row['meta'])
    if mutation == 'actual_cloud_without_provider':
        meta['contract'] = meta.pop('route_contract')
    elif mutation == 'local_model':
        meta['contract']['model'] = 'unapproved-local-model'
    elif mutation == 'route_model':
        meta['route_contract']['provider']['model'] = 'unapproved-cloud-model'
    else:
        meta['model'] = 'unapproved-actual-model'
    led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), row['artifact_id']))
    led.db.commit()
    assert not patient_context_refresh_sources._current_scans(led.db, 1, snapshot['sources'], contract)


def test_dryrun_does_not_count_deferred_or_audit_and_stops_after_completion(runtime, cloud):
    led, _, _ = runtime
    seed(led)
    cloud[1][0] = "dry_run"
    assert step(runtime, local_admit=lambda: False)["status"] == "deferred"
    assert not led.artifacts("cloud_usage_dryrun")
    drain(runtime)
    estimates = len(led.artifacts("cloud_usage_dryrun"))
    assert estimates > 0
    assert step(runtime)["status"] == "complete"
    assert len(led.artifacts("cloud_usage_dryrun")) == estimates


@pytest.mark.parametrize("provider,model,version", [
    ("anthropic", worker.cloud_policy.MODEL, worker.cloud_policy.PRICE_VERSION),
    ("openai", worker.cloud_policy.openai_client.MODEL, worker.cloud_policy.OPENAI_PRICE_VERSION),
])
def test_dryrun_context_records_selected_provider_model_and_price(runtime, cloud, monkeypatch,
                                                                provider, model, version):
    led, _, local_calls = runtime
    seed(led)
    cloud[1][0] = "dry_run"
    monkeypatch.setitem(CFG, "cloud_llm", {"mode": "dry_run", "project_ids": [1],
                                           "provider": provider, "model": model})
    settings, errors = worker.cloud_policy.cloud_config({"cloud_llm": {
        "mode": "dry_run", "project_ids": [1], "provider": provider}})
    assert not errors and settings["model"] == model
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: settings)

    assert step(runtime)["status"] == "progressed"
    estimates = led.artifacts("cloud_usage_dryrun")
    assert len(estimates) == 1
    assert estimates[0]["model"] == model
    meta = json.loads(estimates[0]["meta"])
    assert meta["price_version"] == version and meta["route"] == "patient_context"
    assert estimates[0]["project_id"] is None and estimates[0]["message_id"] is None
    assert local_calls and not cloud[0]
    assert not led.db.in_transaction


def test_cloud_budget_wait_uses_verified_local_context(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    monkeypatch.setattr(worker, "_cloud_mode", lambda *args: "off")
    monkeypatch.setattr(extract_llm_prepare, '_cloud_mode', worker._cloud_mode)
    result = step(runtime)
    assert result['status'] == 'progressed'
    drain(runtime)
    assert local_calls and not cloud[0] and patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    assert step(runtime)['status'] == 'complete'


def test_saved_cloud_context_survives_budget_changes_but_not_code_change(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    settings = {"mode": "on", "project_ids": [1], "routes": ["patient_context"]}
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: worker.cloud_policy.cloud_config(
        {"cloud_llm": settings})[0])
    seed(led, body="見出しのない完全合成の居住記録。")
    drain(runtime)
    before = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    assert before
    settings.update(monthly_cost_limit_usd=8, usd_jpy=140, max_concurrency=3, exclude_project_ids=[2])
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) == before
    assert step(runtime)["status"] == "complete"
    monkeypatch.setattr(worker, "_upgrade_contract", lambda: "f" * 64)
    monkeypatch.setattr(extract_llm_qc, '_upgrade_contract', worker._upgrade_contract)
    monkeypatch.setattr(extract_llm_store, '_upgrade_contract', worker._upgrade_contract)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) == before
    monkeypatch.setattr(worker, "PATIENT_CONTEXT_VERSION", worker.PATIENT_CONTEXT_VERSION + 1)
    monkeypatch.setattr(extract_llm_context, 'PATIENT_CONTEXT_VERSION', worker.PATIENT_CONTEXT_VERSION)
    monkeypatch.setattr(extract_llm_qc, 'PATIENT_CONTEXT_VERSION', worker.PATIENT_CONTEXT_VERSION)
    monkeypatch.setattr(extract_llm_store, 'PATIENT_CONTEXT_VERSION', worker.PATIENT_CONTEXT_VERSION)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None   # this version alone rebuilds
    monkeypatch.setattr(worker, "PATIENT_CONTEXT_VERSION", worker.PATIENT_CONTEXT_VERSION - 1)
    monkeypatch.setattr(extract_llm_context, 'PATIENT_CONTEXT_VERSION', worker.PATIENT_CONTEXT_VERSION)
    monkeypatch.setattr(extract_llm_qc, 'PATIENT_CONTEXT_VERSION', worker.PATIENT_CONTEXT_VERSION)
    monkeypatch.setattr(extract_llm_store, 'PATIENT_CONTEXT_VERSION', worker.PATIENT_CONTEXT_VERSION)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) == before
    monkeypatch.setattr(worker, "EXTRACT_VERSION", worker.EXTRACT_VERSION + 1)
    monkeypatch.setattr(extract_llm_context, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_errors, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_prepare, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_progress, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_proofs, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_provider, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_qc, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_resident, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_selection, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_store, 'EXTRACT_VERSION', worker.EXTRACT_VERSION)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None


@pytest.mark.parametrize("abort", [False, True])
def test_dryrun_estimate_is_atomic_with_context_and_progress(runtime, cloud, monkeypatch, abort):
    led, _, _ = runtime
    seed(led)
    cloud[1][0] = "dry_run"
    record = worker.cloud_policy.dryrun_tx
    def estimate(*args, **kwargs):
        assert led.db.in_transaction
        record(*args, **kwargs)
        assert led.db.in_transaction
        if abort:
            raise RuntimeError("synthetic_save_failure")
    monkeypatch.setattr(worker.cloud_policy, "dryrun_tx", estimate)
    if abort:
        with pytest.raises(RuntimeError, match="synthetic_save_failure"):
            step(runtime)
        for kind in (patient_context_refresh_state.KIND, patient_context_refresh_state.SCAN, "cloud_usage_dryrun"):
            assert not led.artifacts(kind)
        assert led.artifacts(patient_context_refresh_state.PROGRESS)  # Selection is a separate preclaim.
    else:
        assert step(runtime)["status"] == "progressed"
        assert led.artifacts(patient_context_refresh_state.KIND) and led.artifacts(patient_context_refresh_state.PROGRESS)
        assert len(led.artifacts("cloud_usage_dryrun")) == 1
    assert not led.db.in_transaction


def test_summary_comparison_resolves_source_and_reported_subject(runtime):
    led, _, _ = runtime
    seed(led, body="生活環境: 完全合成の居住記録。")
    source = patient_context_refresh_sources._snapshot(led.db, 1, CFG)["sources"][0]
    facts = context_plan.source_facts(source, source["items"], night_id="synthetic", discovery="new_report", found_at=patient_context_refresh_state.time.time())
    assert facts
    first = facts[0]
    other_source = {**source, "message_id": 2}
    other = {**first, "id": "synthetic-other", "message_id": 2}
    left = patient_context_refresh_state._comparison_context([{"text": first["text"], "fact_ids": [first["id"]]}],
        [source, other_source], facts=[first, other])
    right = patient_context_refresh_state._comparison_context([{"text": other["text"], "fact_ids": [other["id"]]}],
        [source, other_source], facts=[first, other])
    assert left != right
    changed = {**first, "reported_subject": "family" if first["reported_subject"] != "family" else "patient"}
    assert patient_context_refresh_state._comparison_context({"facts": [changed]}, [source]) != left


def test_shadow_scan_does_not_count_local_prefix_as_cloud_work(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    cloud[1][0] = "shadow"
    seed(led, body="生活環境: 完全合成の前半記録。\n\n【補足記録】\n" + "完全合成の後半記録です。" * 400)
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG)
    source = snapshot["sources"][0]
    prefix = context_plan.source_facts(source, source["items"], night_id="synthetic", discovery="new_report", found_at=patient_context_refresh_state.time.time())
    assert prefix
    chunks = context_refresh.context_plan.scan_plan(source, {})[0]
    assert len(chunks) > 1
    assert all(f["evidence_span"][1] <= len(chunks[0]) for f in prefix)
    occurrence = patient_context_refresh_sources._source_occurrence(led.db, 1, source)
    saved = {"cursor": 1, "chunks": len(chunks), "complete": False, "audited": False, "facts": prefix,
             "source_occurrence_version": 1, "source_occurrence": occurrence}
    binding = {"source_sig": context_plan.source_signature(source), "source_key": 1,
               "contract": snapshot["contract"], "model": "synthetic",
               "source_occurrence_version": 1, "source_occurrence": occurrence,
               "source_binding": occurrence["source_binding"]}
    with led.db:
        patient_context_refresh_state._replace(led, patient_context_refresh_state.CHUNK, 1,
            {"chunk_span": [0,len(chunks[0])], "part": 0, "parts": 1, "facts": prefix},
            {**binding,"chunk_key":"a" * 64, "unit_key":"b" * 64}, 1)
        patient_context_refresh_state._replace(led, patient_context_refresh_state.SCAN, 1, saved,
            binding, 1)
        patient_context_refresh_state._replace(led, patient_context_refresh_state.PROGRESS, 0, {"cursor": 6}, {})
    answer = {"status": 200, "finish_reason": "stop", "text": '{"patient_context":[]}'}
    monkeypatch.setattr(context_refresh.local_llm, "admitted_chat", lambda *args, **kwargs: answer)
    original = cloud_inference.CloudInferenceUnit
    class Empty(original):
        def __call__(self, prompt, **kwargs):
            return answer
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Empty)
    assert step(runtime)["lane"] == "B"
    comparison = json.loads(led.artifacts("cloud_compare_v1")[0]["meta"])
    assert comparison["local_facts"] == comparison["cloud_facts"] == comparison["matched"] == 0
    assert comparison["units"] == comparison["validated_units"] == 1
    assert comparison["rejected"] == comparison["repairs"] == 0
    assert comparison["held"] is None and comparison["held_observations"] == 0
    stored = document(led, patient_context_refresh_state.SCAN)
    retained = [f for row in led.artifacts(patient_context_refresh_state.CHUNK) for f in json.loads(row['content'])['facts']]
    assert {fact["id"] for fact in prefix} <= {fact['id'] for fact in retained}
    assert not {fact['id'] for fact in prefix}.intersection(fact['id'] for fact in stored['facts'])


def test_pending_estimate_uses_exact_current_contract_without_credentials(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led)
    contract = patient_context_refresh_state._contract(CFG, 1)
    monkeypatch.setattr(worker.cloud_policy, "api_key", lambda *args: pytest.fail("estimate read a credential"))
    estimate = patient_context_refresh_readiness.pending_estimate(led.db, 1, CFG, contract=contract)
    assert estimate["targets"] == estimate["scan_sources"] == 1
    assert estimate["summary_units"] == 0  # Cloud summary waits for source scans and audits.
    assert estimate["scan_chunks_estimate"] >= 1 and estimate["input_chars"] > 0
    cfg = {**CFG, "extract": {"patient_context_nightly": {"enabled": False}}}
    assert not any(patient_context_refresh_readiness.pending_estimate(led.db, 1, cfg, contract=contract).values())


def test_pending_estimate_completed_current_context_has_no_due_work(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led)
    drain(runtime)
    contract = patient_context_refresh_state._contract(CFG, 1)
    monkeypatch.setattr(worker.cloud_policy, "api_key", lambda *args: pytest.fail("estimate read a credential"))
    assert not any(patient_context_refresh_readiness.pending_estimate(led.db, 1, CFG, contract=contract).values())


@pytest.mark.parametrize('fact_source', ['shadow', 'canonical'])
def test_pending_estimate_nonlegacy_does_not_read_provider_key(runtime, cloud, monkeypatch, tmp_path, fact_source):
    import copy
    import mcs_util
    import semantic_candidate
    import semantic_policy
    from candidate_testkit import approved_config

    led, _, _ = runtime
    seed(led)
    drain(runtime)
    contract = patient_context_refresh_state._contract(CFG, 1)
    cfg = copy.deepcopy(CFG)
    cfg['semantic']['fact_source'] = fact_source
    monkeypatch.setattr(mcs_util, 'HOME', str(tmp_path))
    monkeypatch.setattr(semantic_candidate, '_LOADED_SOURCES', None)
    if fact_source == 'canonical':
        cfg = approved_config(cfg)
        assert not semantic_policy.semantic_config(cfg, key_present=True)[1]
    with monkeypatch.context() as patch:
        patch.setattr(worker.cloud_policy, 'api_key', lambda *_: pytest.fail('estimate read a credential'))
        estimate = patient_context_refresh_readiness.pending_estimate(led.db, 1, cfg, contract=contract)
    assert estimate['targets'] == 1 and estimate['summary_units'] == 0
    if fact_source == 'canonical':
        cfg['semantic']['match_threshold'] = 0.99
        assert semantic_policy.semantic_config(cfg, key_present=True)[1]
        doc, meta = patient_context_refresh_state._latest(led.db, patient_context_refresh_state.KIND, 1)
        assert not patient_context_refresh_readiness._quality_publication_allowed(led.db, 1, cfg, doc, meta)


def test_scope_withdrawn_while_waiting_for_writer_records_disposition(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led)
    allowed = [True]
    attempts = []
    def locked():
        attempts.append(1)
        with led.db:
            led.db.execute("UPDATE messages SET body_text=body_text || '訂正' WHERE message_id=1")
        allowed[0] = False
        return None
    monkeypatch.setattr(patient_context_refresh_state, "acquire_run_lock", locked)
    result = step(runtime, admit=lambda: allowed[0])
    assert result["status"] == "deferred" and result["reason"] == "run_lock"
    assert cloud[0] and not document(led)
    assert led.synthetic_cloud_dispositions == ["discarded_scope"]


def test_context_shadow_post_validation_revocation_records_discard(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[1][0] = "shadow"
    original = cloud_inference.CloudInferenceUnit
    class RevokedAfterResponse(original):
        def __call__(self, *args, **kwargs):
            answer = super().__call__(*args, **kwargs)
            self.revoked = True
            return answer
        def commit_guard(self):
            if getattr(self, "revoked", False):
                raise cloud_inference.CloudFallback("cloud_unit_changed")
            return super().commit_guard()
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", RevokedAfterResponse)
    for _ in range(5):
        assert step(runtime)["status"] == "progressed"
        if led.synthetic_cloud_dispositions:
            break
    assert led.synthetic_cloud_dispositions == ["discarded_scope"]
    comparisons = [json.loads(row["meta"]) for row in led.artifacts("cloud_compare_v1")]
    assert sum(item["cloud_facts"] is None and item["validated_units"] == 0 for item in comparisons) == 1
    for row in led.db.execute("SELECT meta FROM artifacts WHERE kind IN (?,?)", (patient_context_refresh_state.KIND, patient_context_refresh_state.SCAN)):
        assert "provider" not in json.loads(row[0])


@pytest.mark.parametrize('invalid', [(), ('local',), ('cloud',), ('local','cloud')])
def test_shadow_grounding_sides_remain_independently_measured(runtime, cloud, monkeypatch, invalid):
    led, _, _ = runtime
    cloud[1][0] = 'shadow'
    seed(led,body='完全合成の見出しなし記録です。')
    assert step(runtime)['lane'] == 'A'
    prior_rows = led.artifacts("cloud_compare_v1")
    prior_ids = {row["artifact_id"] for row in prior_rows}
    values = {}
    scan, compare = patient_context_quality_controller._scan, patient_context_refresh_state._comparison_context
    def measured_scan(*args, **kwargs):
        value = scan(*args, **kwargs)
        values['cloud' if kwargs.get('provider') is not None else 'local'] = value
        return value
    def grounded(value, *args, **kwargs):
        if any(value is values.get(side) for side in invalid):
            raise ValueError('cloud_comparison_source_invalid')
        return compare(value,*args,**kwargs)
    monkeypatch.setattr(patient_context_quality_controller,'_scan',measured_scan)
    monkeypatch.setattr(patient_context_refresh_state,'_comparison_context',grounded)
    assert step(runtime)['lane'] == 'B'
    all_rows = led.artifacts('cloud_compare_v1')
    assert [row for row in all_rows if row['artifact_id'] in prior_ids] == prior_rows
    rows = [row for row in all_rows if row['artifact_id'] not in prior_ids]
    assert len(rows)==1
    meta = json.loads(rows[0]['meta'])
    assert meta['unit'] and 'uncompared' not in meta
    assert meta['units']==1
    assert meta['local_facts'] == (None if 'local' in invalid else 1)
    assert meta['cloud_facts'] == (None if 'cloud' in invalid else 1)
    assert meta['validated_units'] == int('cloud' not in invalid)
    assert meta['matched'] == (None if invalid else 1)


@pytest.mark.parametrize('finish', ['busy','deadline'])
def test_shadow_audit_is_a_single_short_lock_attempt_and_keeps_pending_when_unavailable(runtime, cloud, monkeypatch, finish):
    led, clock, _ = runtime
    cloud[1][0] = 'shadow'
    seed(led,body='完全合成の見出しなし記録です。')
    assert step(runtime)['lane'] == 'A'
    prior_rows = led.artifacts("cloud_compare_v1")
    prior_ids = {row["artifact_id"] for row in prior_rows}
    attempts, held = [], set()
    close = os.close
    def acquire():
        attempts.append(1)
        if len(attempts)>1:  # the local result commit
            return None
        fd = os.open(os.devnull,os.O_RDONLY)
        held.add(fd)
        return fd
    def release(fd):
        held.discard(fd)
        close(fd)
    original_provider = cloud_inference.CloudInferenceUnit
    class Provider(original_provider):
        def __call__(self,*args,**kwargs):
            assert held == set(), 'shadow HTTP must not retain the local commit lock'
            return super().__call__(*args,**kwargs)
    compare = patient_context_refresh_state._comparison_context
    def grounded(*args,**kwargs):
        if finish=='deadline' and cloud[0]:
            clock.mono += 1000
        return compare(*args,**kwargs)
    monkeypatch.setattr(patient_context_refresh_state,'acquire_run_lock',acquire)
    monkeypatch.setattr(context_refresh.os,'close',release)
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit',Provider)
    monkeypatch.setattr(patient_context_refresh_state,'_comparison_context',grounded)
    assert step(runtime)['lane']=='B'
    assert len(attempts)==(2 if finish=='busy' else 1) and held==set()
    all_rows = led.artifacts('cloud_compare_v1')
    assert [row for row in all_rows if row['artifact_id'] in prior_ids] == prior_rows
    rows = [row for row in all_rows if row['artifact_id'] not in prior_ids]
    assert len(rows)==1 and cloud[0]
    meta = json.loads(rows[0]['meta'])
    assert meta['units']==1 and meta['uncompared']=='pending' and meta['validated_units']==0
    assert step(runtime,admit=lambda:False)['status']=='deferred'
    assert led.artifacts('cloud_compare_v1')==all_rows


def test_shadow_pending_is_atomic_with_local_success_and_replay_does_not_duplicate(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    cloud[1][0] = "shadow"
    seed(led, body="完全合成の見出しなし記録です。")
    assert step(runtime)["lane"] == "A"
    prior_rows = led.artifacts("cloud_compare_v1")
    prior_ids = {row["artifact_id"] for row in prior_rows}
    before = [dict(row) for row in led.db.execute("SELECT * FROM artifacts WHERE kind!=? ORDER BY artifact_id", (patient_context_refresh_state.PROGRESS,))]
    def fail(*args, **kwargs):
        raise ValueError("synthetic_pending_failure")
    with monkeypatch.context() as patch:
        patch.setattr(worker, "_cloud_compare_tx", fail)
        patch.setattr(extract_llm_provider, '_cloud_compare_tx', worker._cloud_compare_tx)
        patch.setattr(extract_llm_store, '_cloud_compare_tx', worker._cloud_compare_tx)
        with pytest.raises(ValueError, match="synthetic_pending_failure"):
            step(runtime)
    assert [dict(row) for row in led.db.execute("SELECT * FROM artifacts WHERE kind!=? ORDER BY artifact_id", (patient_context_refresh_state.PROGRESS,))] == before
    assert not cloud[0]
    assert step(runtime)["lane"] == "B"
    row, = [row for row in led.artifacts("cloud_compare_v1") if row["artifact_id"] not in prior_ids]
    meta = json.loads(row["meta"])
    assert meta["unit"] and "uncompared" not in meta
    with led.db:
        worker._cloud_compare_tx(led, route="patient_context", project_id=1,
            left=None, right=None, rejected=None, held=None, repairs=None, unit=meta["unit"], pending=True)
    worker._cloud_compare(led, route="patient_context", project_id=1,
        local_facts=None, cloud_facts=None, unit=meta["unit"])
    assert {r["artifact_id"] for r in led.artifacts("cloud_compare_v1")} == prior_ids | {row["artifact_id"]}
    assert [r for r in led.artifacts("cloud_compare_v1") if r["artifact_id"] in prior_ids] == prior_rows


def test_paid_cloud_failure_without_local_capacity_records_the_retry(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    sent = []
    original = cloud_inference.CloudInferenceUnit
    class Paid(original):
        def __call__(self, prompt, **kwargs):
            sent.append(prompt)
            self.possibly_sent = True
            raise cloud_inference.CloudFallback("synthetic_failure")
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Paid)
    for _ in range(3):   # the rotation reaches the scan (lane B) after the fact-less summary
        result = step(runtime, local_admit=lambda: False)
        if result["status"] == "failed":
            break
    assert result["status"] == "failed" and result["reason"] == "nightly_cloud_failed" and result["lane"] == "B"
    assert not local_calls and len(sent) == 1
    stored = document(led, patient_context_refresh_state.SCAN)
    assert stored["failure"] == "nightly_cloud_failed" and stored["attempts"] == 1 and stored["next_try"] > 0
    # The failed attempt remains bound to the requested cloud route and source.
    meta = json.loads(led.db.execute("SELECT meta FROM artifacts WHERE kind=?", (patient_context_refresh_state.SCAN,)).fetchone()[0])
    contract = patient_context_refresh_state._contract(CFG, 1)
    assert meta["provider"] == contract["provider"] and meta["model"] == contract["provider"]["model"]
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG)
    saved = patient_context_refresh_sources._current_scans(led.db, 1, snapshot["sources"], contract)[1]
    assert saved["failure"] == "nightly_cloud_failed" and saved["attempts"] == 1 and saved["next_try"] > 0
    # the backoff keeps the next tick from re-sending the same unit
    assert step(runtime, local_admit=lambda: False)["status"] != "failed" and len(sent) == 1


@pytest.mark.parametrize('local_ready', [False, True])
def test_unsettled_cloud_attempt_without_local_capacity_records_the_retry(runtime, cloud, monkeypatch, local_ready):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    calls = []
    original = cloud_inference.CloudInferenceUnit
    class Unknown(original):
        def __call__(self, prompt, **kwargs):
            calls.append(True)
            self.possibly_sent = True
            raise cloud_inference.CloudFallback("cloud_unit_failed")
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Unknown)
    for _ in range(3):
        result = step(runtime, local_admit=lambda: local_ready)
        if result["status"] == "failed":
            break
    assert result["status"] == "failed" and result["reason"] == "nightly_cloud_failed"
    scan = document(led, patient_context_refresh_state.SCAN)
    assert scan["attempts"] == 1 and scan["next_try"] > 0
    assert calls == [True] and not local_calls
    assert step(runtime, local_admit=lambda: False)["status"] != "failed" and calls == [True]


@pytest.mark.parametrize('source_changed', [False, True])
def test_scope_change_after_unknown_attempt_fences_retry_to_source(runtime, cloud, monkeypatch, source_changed):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    calls = []
    original = cloud_inference.CloudInferenceUnit
    class Changed(original):
        def __call__(self, prompt, **kwargs):
            self.possibly_sent = True
            calls.append(True)
            if source_changed:
                led.save_messages([_message(1, "完全合成の別の新しい本文。", project_id=1)])
            raise cloud_inference.CloudFallback("cloud_unit_changed")
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Changed)
    for _ in range(3):
        result = step(runtime, local_admit=lambda: False)
        if calls:
            break
    assert calls == [True] and not local_calls
    scan = document(led, patient_context_refresh_state.SCAN)
    if source_changed:
        assert result["status"] == "source_changed" and scan is None
    else:
        assert result["status"] == "failed" and result["reason"] == "nightly_cloud_failed"
        assert scan["attempts"] == 1 and scan["next_try"] > 0
        assert step(runtime, local_admit=lambda: False)["status"] != "failed" and calls == [True]


def test_paid_failure_of_a_partial_cloud_scan_keeps_its_cloud_pin(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG)
    meta = {"source_sig": context_plan.source_signature(snapshot["sources"][0]),
            "contract": snapshot["contract"], "provider": snapshot["contract"]["provider"]}
    with led.db:
        patient_context_refresh_state._replace(led, patient_context_refresh_state.SCAN, 1,
                                 {"cursor": 1, "chunks": 2, "complete": False, "facts": []}, meta, 1)
    sent = []
    original = cloud_inference.CloudInferenceUnit
    class Paid(original):
        def __call__(self, prompt, **kwargs):
            sent.append(prompt)
            self.possibly_sent = True
            raise cloud_inference.CloudFallback("synthetic_failure")
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Paid)
    for _ in range(3):
        result = step(runtime, local_admit=lambda: False)
        if result["status"] == "failed":
            break
    assert result["status"] == "failed" and result["reason"] == "nightly_cloud_failed"
    assert not local_calls and len(sent) == 1
    # the retry stays on the cloud backend: no local record of a cloud cursor
    scan = patient_context_refresh_sources._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])[1]
    assert scan["_provider_contract"] == snapshot["contract"]["provider"]
    assert scan["failure"] == "nightly_cloud_failed" and scan["next_try"] > 0


def test_shadow_unit_stays_pending_when_the_run_lock_is_busy_after_measuring(runtime, cloud, monkeypatch):
    led, clock, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[1][0] = "shadow"
    measured = [False]
    original = cloud_inference.CloudInferenceUnit
    class Measured(original):
        def __call__(self, prompt, **kwargs):
            value = super().__call__(prompt, **kwargs)
            measured[0] = True
            clock.mono += 600          # the lock wait runs into the unit deadline
            return value
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Measured)
    monkeypatch.setattr(patient_context_refresh_state, "acquire_run_lock",
                        lambda: None if measured[0] else os.open(os.devnull, os.O_RDONLY))
    for _ in range(3):   # the rotation reaches the scan (lane B) after the fact-less summary
        result = step(runtime)
        if measured[0]:
            break
    assert result["status"] == "progressed" and local_calls and measured[0]
    # the pending row was written with the local commit; the measured result
    # could not be written under the run lock, so the unit stays uncompared
    rows = [json.loads(a["meta"]) for a in led.artifacts("cloud_compare_v1")]
    [row] = [r for r in rows if r.get("uncompared") == "pending"]
    assert row["units"] == 1 and row["validated_units"] == 0


def test_quality_policy_change_requeues_audit_without_resending_source_or_draft(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body='本人の生活環境: 完全合成の居住記録。')
    drain(runtime)
    chunks = [(row['artifact_id'], row['content'], row['meta'])
              for row in led.artifacts(patient_context_refresh_state.CHUNK)]
    generated = list(cloud[0])
    judged = len(led.synthetic_jev_requests)
    usage_count = len(led.artifacts('semantic_usage', project_id=1))
    monkeypatch.setitem(CFG['semantic'], 'match_threshold', 0.95)
    _semantic_proof(led, 1, 1, CFG)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
    _, _, pending = patient_context_refresh_readiness._pending(led.db, 1, CFG)
    assert pending == {'A': False, 'B': False, 'C': True}
    drain(runtime)
    requests = led.synthetic_jev_requests[judged:]
    assert [request['state']['target']['id'] for request in requests] == [
        'source_facts', 'source_coverage', 'overview']
    usage = led.artifacts('semantic_usage', project_id=1)[usage_count:]
    assert [json.loads(row['meta'])['operation'] for row in usage] == [
        'patient_source_quality', 'patient_source_quality', 'patient_summary_quality']
    assert cloud[0] == generated
    assert [(row['artifact_id'], row['content'], row['meta'])
            for row in led.artifacts(patient_context_refresh_state.CHUNK)] == chunks
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)


@pytest.mark.parametrize('state', ['unknown', 'held'])
def test_quality_hold_reason_is_visible_and_clears_only_after_success(runtime, cloud, monkeypatch, state):
    led, clock, _ = runtime
    seed(led, body='本人の生活環境: 完全合成の居住記録。')
    for _ in range(8):
        step(runtime)
        if document(led):
            break
    assert document(led) and not led.artifacts(patient_context_refresh_state.QUALITY_PASS)
    with monkeypatch.context() as patch:
        patch.setattr(patient_context_quality_controller, '_quality_step', lambda *a, **k: (state, None))
        result = step(runtime)
    assert result['status'] == 'deferred' and result['reason'] == 'summary_quality_' + state
    assert document(led, patient_context_refresh_state.PROGRESS)['quality_state'] == state
    proof = patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)
    assert not proof['complete'] and proof['status'] == 'quality_' + state
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
    clock.wall += 61
    clock.mono += 61
    drain(runtime)
    assert 'quality_state' not in document(led, patient_context_refresh_state.PROGRESS)
    assert patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']


def test_read_proof_cannot_outlive_its_owner_even_with_a_borrowed_transaction(runtime, cloud):
    led, _, _ = runtime
    seed(led)
    drain(runtime)
    led.db.execute('BEGIN')
    try:
        with patient_context_refresh_sources._scan_read_owner(led.db, 1) as proof_for:
            snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG, hydrate=False)
            proof = proof_for(snapshot, CFG)
            assert patient_context_refresh_sources._scans_from_read(led.db, 1, snapshot['sources'], snapshot['contract'], CFG, proof)[1]['audited']
        assert led.db.in_transaction
        with pytest.raises(RuntimeError, match='patient_context_read_changed'):
            patient_context_refresh_sources._scans_from_read(led.db, 1, snapshot['sources'], snapshot['contract'], CFG, proof)
    finally:
        led.db.rollback()
