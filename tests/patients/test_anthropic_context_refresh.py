"""Synthetic per-unit context routing; no keys, GPU, HTTP or source data."""
import json
import os

import pytest

import extract_llm as worker
import patient_context_refresh as context_refresh
from test_patient_context_refresh import CFG, document, runtime, seed, stamp, step

__all__ = ["runtime"]


@pytest.fixture
def cloud(runtime, monkeypatch):
    led, clock, local_calls = runtime
    requests = []
    mode = ["on"]
    failure = [False]
    discarded = []
    led.synthetic_cloud_dispositions = discarded
    monkeypatch.setattr(worker.cloud_policy, "api_key", lambda root=None: "synthetic-key")
    monkeypatch.setattr(worker, "_cloud_settings", lambda cfg: worker.cloud_policy.cloud_config(
        {"cloud_llm": {"mode": mode[0], "project_ids": [1]}})[0])
    monkeypatch.setattr(worker, "_cloud_mode", lambda *args: mode[0])
    class Provider:
        model = "claude-haiku-5-5"
        def __init__(self, ledger, cfg, **kwargs):
            assert ledger is led
            self.current = kwargs["current"]
            self.discard_reason = kwargs.get("discard_reason")
            self.contract = worker._cloud_contract()
        def guard(self):
            if not self.current():
                raise worker.CloudFallback("synthetic_stale")
        def commit_guard(self):
            return self.guard()
        def discard(self):
            assert not led.db.in_transaction
            discarded.append(self.discard_reason() if self.discard_reason else "discarded_scope")
        def __call__(self, prompt, **kwargs):
            self.guard()
            if failure[0]:
                raise worker.CloudFallback("synthetic_failure")
            payload = json.loads(prompt)
            requests.append(payload)
            if "reports" in payload:
                result = {"fact_ids": [item["id"] for item in payload["reports"][:12]]}
            else:
                body = payload["source"]
                result = {"patient_context": [{"category": "living", "text": body,
                    "evidence": body, "subject": "unspecified", "details": []}]}
            return {"status": 200, "finish_reason": "stop", "text": json.dumps(result)}
    monkeypatch.setattr(worker, "_CloudProvider", Provider)
    return requests, mode, failure


def drain(runtime, *, local_admit=lambda: True):
    for _ in range(30):
        result = step(runtime, local_admit=local_admit)
        if result["status"] == "complete":
            return
        assert result["status"] == "progressed", result
    pytest.fail("synthetic context did not finish")


def test_daytime_cloud_needs_no_local_gpu_capacity(runtime, cloud):
    led, clock, local_calls = runtime
    clock.wall = stamp("2026-10-18T12:00:00")
    seed(led, body="見出しのない完全合成の居住記録。")
    assert led.db.execute("SELECT first_seen FROM messages WHERE message_id=1").fetchone()[0] <= clock.wall
    drain(runtime, local_admit=lambda: False)
    assert cloud[0] and not local_calls
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)
    scan = led.db.execute("SELECT meta FROM artifacts WHERE kind=?", (context_refresh.SCAN,)).fetchone()
    meta = json.loads(scan[0])
    assert meta["provider"] == worker._cloud_contract()
    assert meta["model"] == "claude-haiku-5-5"
    assert meta["contract"] == context_refresh._contract(CFG, 1)


def test_failed_cloud_retries_local_with_its_model(runtime, cloud):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[2][0] = True
    drain(runtime)
    assert local_calls and not cloud[0]
    for row in led.db.execute("SELECT meta FROM artifacts WHERE kind IN (?,?)", (context_refresh.KIND, context_refresh.SCAN)):
        meta = json.loads(row[0])
        assert "provider" not in meta and meta["model"] == "synthetic"
        assert "provider" not in meta["contract"]
        assert meta["route_contract"] == context_refresh._contract(CFG, 1)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert step(runtime)["status"] == "complete"


def test_failed_cloud_does_not_bypass_local_admission(runtime, cloud):
    led, _, local_calls = runtime
    seed(led)
    # An explicit source fact makes the first summary actually call its provider.
    assert context_refresh._snapshot(led.db, 1, CFG)["sources"][0]["items"]
    cloud[2][0] = True
    result = step(runtime, local_admit=lambda: False)
    assert result["status"] == "deferred"
    assert not local_calls and document(led) is None


def test_shadow_runs_only_after_local_commit_and_never_publishes_cloud(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[1][0] = "shadow"
    original = worker._CloudProvider
    class Shadow(original):
        def __call__(self, prompt, **kwargs):
            assert not led.db.in_transaction
            payload = json.loads(prompt)
            kind = context_refresh.KIND if "reports" in payload else context_refresh.SCAN
            assert led.db.execute("SELECT 1 FROM artifacts WHERE kind=?", (kind,)).fetchone()
            return super().__call__(prompt, **kwargs)
    monkeypatch.setattr(worker, "_CloudProvider", Shadow)
    drain(runtime)
    assert local_calls and cloud[0]
    assert led.artifacts("cloud_compare_v1")
    for row in led.db.execute("SELECT meta FROM artifacts WHERE kind IN (?,?)", (context_refresh.KIND, context_refresh.SCAN)):
        assert "provider" not in json.loads(row[0])


def test_crossing_midnight_preserves_cloud_publication_and_progress(runtime, cloud, monkeypatch):
    led, clock, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    clock.wall = stamp("2026-10-17T23:59:30")
    original = worker._CloudProvider
    class Crossing(original):
        def __call__(self, prompt, **kwargs):
            answer = super().__call__(prompt, **kwargs)
            clock.wall = stamp("2026-10-18T00:00:01")
            clock.mono += 31
            return answer
    monkeypatch.setattr(worker, "_CloudProvider", Crossing)
    # First unit publishes the empty deterministic selection; the scan sends next.
    step(runtime)
    result = step(runtime)
    assert result["status"] == "progressed"
    assert led.artifacts(context_refresh.SCAN)
    meta = led.db.execute("SELECT meta FROM artifacts WHERE kind=? AND project_id=1", (context_refresh.SCAN,)).fetchone()
    assert json.loads(meta[0])["night_id"] == "2026-10-17"
    assert not led.synthetic_cloud_dispositions
    before = len(cloud[0])
    drain(runtime)
    after = len(cloud[0])
    assert after >= before
    assert step(runtime)["status"] == "complete" and len(cloud[0]) == after


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_cloud_completed_data_is_not_reused_after_route_change(runtime, cloud, mode):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    drain(runtime)
    assert not local_calls
    cloud[1][0] = mode
    # the published context stays readable while its backend's request shape is
    # current; only the rebuild under the new route is due
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)
    snapshot, _, pending = context_refresh._pending(led.db, 1, CFG)
    assert pending["A"] and pending["B"]
    assert not context_refresh._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])
    drain(runtime)
    assert local_calls and context_refresh.context_view_input(led.db, 1, cfg=CFG)
    for row in led.db.execute("SELECT meta FROM artifacts WHERE kind IN (?,?)", (context_refresh.KIND, context_refresh.SCAN)):
        assert "provider" not in json.loads(row[0])


def test_partial_cloud_scan_requires_same_rules_and_provider_contract(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led)
    snapshot = context_refresh._snapshot(led.db, 1, CFG)
    source = snapshot["sources"][0]
    meta = {"source_sig": context_refresh._source_sig(source), "contract": snapshot["contract"],
            "provider": {**snapshot["contract"]["provider"], "prompt": "0" * 16}}
    with led.db:
        context_refresh._replace(led, context_refresh.SCAN, 1, {"cursor": 1, "complete": False, "facts": []}, meta, 1)
    assert not context_refresh._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])
    meta["provider"] = snapshot["contract"]["provider"]
    with led.db:
        context_refresh._replace(led, context_refresh.SCAN, 1, {"cursor": 1, "complete": False, "facts": []}, meta, 1)
    assert context_refresh._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])[1]["cursor"] == 1
    # a code release that leaves the inference rules alone keeps the scan
    monkeypatch.setattr(worker, "_upgrade_contract", lambda: "f" * 64)
    assert context_refresh._snapshot(led.db, 1, CFG)["contract"] == snapshot["contract"]
    # an inference rule version change rebuilds it
    monkeypatch.setattr(worker, "EXTRACT_VERSION", worker.EXTRACT_VERSION + 1)
    changed, _, pending = context_refresh._pending(led.db, 1, CFG)
    assert pending["B"] and changed["contract"] != snapshot["contract"]
    assert not context_refresh._current_scans(led.db, 1, changed["sources"], changed["contract"])


def test_scope_is_rechecked_after_writer_acquisition(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    # Publish the deterministic empty summary before revoking the next paid scan.
    # Otherwise both steps reject the same unsent summary, never exercising HTTP.
    assert step(runtime)["status"] == "progressed"
    assert not cloud[0] and not led.synthetic_cloud_dispositions
    original = worker._CloudProvider
    checked = []
    class RevokedAtCommit(original):
        def guard(self):
            checked.append(led.db.in_transaction)
            if led.db.in_transaction:
                raise worker.CloudFallback("synthetic_changed_while_waiting")
            return super().guard()
    monkeypatch.setattr(worker, "_CloudProvider", RevokedAtCommit)
    result = step(runtime)
    assert result["status"] == "deferred" and len(cloud[0]) == 1
    assert False in checked and True in checked
    assert not led.artifacts(context_refresh.SCAN)
    assert led.synthetic_cloud_dispositions == ["discarded_scope"]


@pytest.mark.parametrize("mutation", ["actual_cloud_without_provider", "local_model", "route_model", "actual_model"])
def test_fallback_reuse_requires_exact_local_and_route_contract(runtime, cloud, mutation):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[2][0] = True
    drain(runtime)
    row = led.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind=?", (context_refresh.SCAN,)).fetchone()
    meta = json.loads(row["meta"])
    if mutation == "actual_cloud_without_provider":
        meta["contract"] = meta.pop("route_contract")
    elif mutation == "local_model":
        meta["contract"]["model"] = "unapproved-local-model"
    elif mutation == "route_model":
        meta["route_contract"]["provider"]["model"] = "unapproved-cloud-model"
    else:
        meta["model"] = "unapproved-actual-model"
    led.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), row["artifact_id"]))
    led.db.commit()
    snapshot = context_refresh._snapshot(led.db, 1, CFG)
    assert not context_refresh._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])


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



def test_budget_fallback_saves_local_contract_and_remains_reusable(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    # The requested route remains cloud; admission blocks new paid work.
    monkeypatch.setattr(worker, "_cloud_mode", lambda *args: "off")
    drain(runtime)
    assert local_calls and not cloud[0]
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)
    for row in led.db.execute("SELECT meta FROM artifacts WHERE kind IN (?,?)", (context_refresh.KIND, context_refresh.SCAN)):
        meta = json.loads(row[0])
        assert "provider" not in meta["contract"] and "provider" not in meta
        assert meta["route_contract"] == context_refresh._contract(CFG, 1)
    assert step(runtime)["status"] == "complete"


def test_saved_cloud_context_survives_budget_changes_but_not_code_change(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    settings = {"mode": "on", "project_ids": [1]}
    monkeypatch.setattr(worker, "_cloud_settings", lambda cfg: worker.cloud_policy.cloud_config(
        {"cloud_llm": settings})[0])
    seed(led, body="見出しのない完全合成の居住記録。")
    drain(runtime)
    before = context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert before
    settings.update(monthly_cost_limit_usd=8, usd_jpy=140, max_concurrency=3, exclude_project_ids=[2])
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) == before
    assert step(runtime)["status"] == "complete"
    monkeypatch.setattr(worker, "_upgrade_contract", lambda: "f" * 64)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) == before
    monkeypatch.setattr(worker, "PATIENT_CONTEXT_VERSION", worker.PATIENT_CONTEXT_VERSION + 1)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None   # this version alone rebuilds
    monkeypatch.setattr(worker, "PATIENT_CONTEXT_VERSION", worker.PATIENT_CONTEXT_VERSION - 1)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) == before
    monkeypatch.setattr(worker, "EXTRACT_VERSION", worker.EXTRACT_VERSION + 1)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None


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
        for kind in (context_refresh.KIND, context_refresh.SCAN, context_refresh.PROGRESS, "cloud_usage_dryrun"):
            assert not led.artifacts(kind)
    else:
        assert step(runtime)["status"] == "progressed"
        assert led.artifacts(context_refresh.KIND) and led.artifacts(context_refresh.PROGRESS)
        assert len(led.artifacts("cloud_usage_dryrun")) == 1
    assert not led.db.in_transaction



def test_summary_comparison_resolves_source_and_reported_subject(runtime):
    led, _, _ = runtime
    seed(led, body="生活環境: 完全合成の居住記録。")
    source = context_refresh._snapshot(led.db, 1, CFG)["sources"][0]
    facts = context_refresh._facts(source, source["items"], night_id="synthetic", discovery="new_report")
    assert facts
    first = facts[0]
    other_source = {**source, "message_id": 2}
    other = {**first, "id": "synthetic-other", "message_id": 2}
    left = context_refresh._comparison_context([{"text": first["text"], "fact_ids": [first["id"]]}],
        [source, other_source], facts=[first, other])
    right = context_refresh._comparison_context([{"text": other["text"], "fact_ids": [other["id"]]}],
        [source, other_source], facts=[first, other])
    assert left != right
    changed = {**first, "reported_subject": "family" if first["reported_subject"] != "family" else "patient"}
    assert context_refresh._comparison_context({"facts": [changed]}, [source]) != left


def test_shadow_scan_does_not_count_local_prefix_as_cloud_work(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    cloud[1][0] = "shadow"
    seed(led, body="生活環境: 完全合成の前半記録。\n\n【補足記録】\n" + "完全合成の後半記録です。" * 400)
    snapshot = context_refresh._snapshot(led.db, 1, CFG)
    source = snapshot["sources"][0]
    prefix = context_refresh._facts(source, source["items"], night_id="synthetic", discovery="new_report")
    assert prefix
    chunks = context_refresh.context_plan.scan_plan(source, {})[0]
    assert len(chunks) > 1
    assert all(f["evidence_span"][1] <= len(chunks[0]) for f in prefix)
    saved = {"cursor": 1, "chunks": len(chunks), "complete": False, "audited": False, "facts": prefix}
    binding = {"source_sig": context_refresh._source_sig(source), "source_key": 1,
               "contract": snapshot["contract"], "model": "synthetic"}
    with led.db:
        context_refresh._replace(led, context_refresh.CHUNK, 1,
            {"chunk_span": [0,len(chunks[0])], "part": 0, "parts": 1, "facts": prefix},
            {**binding,"chunk_key":"synthetic-prefix", "unit_key":"synthetic-unit"}, 1)
        context_refresh._replace(led, context_refresh.SCAN, 1, saved,
            binding, 1)
        context_refresh._replace(led, context_refresh.PROGRESS, 0, {"cursor": 6}, {})
    answer = {"status": 200, "finish_reason": "stop", "text": '{"patient_context":[]}'}
    monkeypatch.setattr(context_refresh.local_llm, "admitted_chat", lambda *args, **kwargs: answer)
    original = worker._CloudProvider
    class Empty(original):
        def __call__(self, prompt, **kwargs):
            return answer
    monkeypatch.setattr(worker, "_CloudProvider", Empty)
    assert step(runtime)["lane"] == "B"
    comparison = json.loads(led.artifacts("cloud_compare_v1")[0]["meta"])
    assert comparison["local_facts"] == comparison["cloud_facts"] == comparison["matched"] == 0
    assert comparison["units"] == comparison["validated_units"] == 1
    assert comparison["rejected"] == comparison["repairs"] == 0
    assert comparison["held"] is None and comparison["held_observations"] == 0
    stored = document(led, context_refresh.SCAN)
    retained = [f for row in led.artifacts(context_refresh.CHUNK) for f in json.loads(row['content'])['facts']]
    assert {fact["id"] for fact in prefix} <= {fact['id'] for fact in retained}
    assert not {fact['id'] for fact in prefix}.intersection(fact['id'] for fact in stored['facts'])






def test_pending_estimate_uses_exact_current_contract_without_credentials(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led)
    contract = context_refresh._contract(CFG, 1)
    monkeypatch.setattr(worker.cloud_policy, "api_key", lambda *args: pytest.fail("estimate read a credential"))
    estimate = context_refresh.pending_estimate(led.db, 1, CFG, contract=contract)
    assert estimate["targets"] == estimate["summary_units"] == estimate["scan_sources"] == 1
    assert estimate["scan_chunks_estimate"] >= 1 and estimate["input_chars"] > 0
    cfg = {**CFG, "extract": {"patient_context_nightly": {"enabled": False}}}
    assert not any(context_refresh.pending_estimate(led.db, 1, cfg, contract=contract).values())



def test_pending_estimate_completed_current_context_has_no_due_work(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led)
    drain(runtime)
    contract = context_refresh._contract(CFG, 1)
    monkeypatch.setattr(worker.cloud_policy, "api_key", lambda *args: pytest.fail("estimate read a credential"))
    assert not any(context_refresh.pending_estimate(led.db, 1, CFG, contract=contract).values())



def test_scope_withdrawn_while_waiting_for_writer_records_disposition(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led)
    allowed = [True]
    def locked():
        allowed[0] = False
        return None
    monkeypatch.setattr(context_refresh, "acquire_run_lock", locked)
    result = step(runtime, admit=lambda: allowed[0])
    assert result["status"] == "deferred" and result["reason"] == "run_lock"
    assert cloud[0] and not document(led)
    assert led.synthetic_cloud_dispositions == ["discarded_scope"]


def test_context_shadow_post_validation_revocation_records_discard(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[1][0] = "shadow"
    original = worker._CloudProvider
    class RevokedAfterResponse(original):
        def __call__(self, *args, **kwargs):
            answer = super().__call__(*args, **kwargs)
            self.revoked = True
            return answer
        def commit_guard(self):
            if getattr(self, "revoked", False):
                raise worker.CloudFallback("cloud_unit_changed")
            return super().commit_guard()
    monkeypatch.setattr(worker, "_CloudProvider", RevokedAfterResponse)
    for _ in range(5):
        assert step(runtime)["status"] == "progressed"
        if led.synthetic_cloud_dispositions:
            break
    assert led.synthetic_cloud_dispositions == ["discarded_scope"]
    comparisons = [json.loads(row["meta"]) for row in led.artifacts("cloud_compare_v1")]
    assert sum(item["cloud_facts"] is None and item["validated_units"] == 0 for item in comparisons) == 1
    for row in led.db.execute("SELECT meta FROM artifacts WHERE kind IN (?,?)", (context_refresh.KIND, context_refresh.SCAN)):
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
    scan, compare = context_refresh._scan, context_refresh._comparison_context
    def measured_scan(*args, **kwargs):
        value = scan(*args, **kwargs)
        values['cloud' if kwargs.get('provider') is not None else 'local'] = value
        return value
    def grounded(value, *args, **kwargs):
        if any(value is values.get(side) for side in invalid):
            raise ValueError('cloud_comparison_source_invalid')
        return compare(value,*args,**kwargs)
    monkeypatch.setattr(context_refresh,'_scan',measured_scan)
    monkeypatch.setattr(context_refresh,'_comparison_context',grounded)
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
        if len(attempts)>1:
            return None
        fd = os.open(os.devnull,os.O_RDONLY)
        held.add(fd)
        return fd
    def release(fd):
        held.discard(fd)
        close(fd)
    original_provider = worker._CloudProvider
    class Provider(original_provider):
        def __call__(self,*args,**kwargs):
            assert held == set(), 'shadow HTTP must not retain the local commit lock'
            return super().__call__(*args,**kwargs)
    compare = context_refresh._comparison_context
    def grounded(*args,**kwargs):
        if finish=='deadline' and cloud[0]:
            clock.mono += 1000
        return compare(*args,**kwargs)
    monkeypatch.setattr(context_refresh,'acquire_run_lock',acquire)
    monkeypatch.setattr(context_refresh.os,'close',release)
    monkeypatch.setattr(worker,'_CloudProvider',Provider)
    monkeypatch.setattr(context_refresh,'_comparison_context',grounded)
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
    before = [dict(row) for row in led.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
    def fail(*args, **kwargs):
        raise ValueError("synthetic_pending_failure")
    with monkeypatch.context() as patch:
        patch.setattr(worker, "_cloud_compare_tx", fail)
        with pytest.raises(ValueError, match="synthetic_pending_failure"):
            step(runtime)
    assert [dict(row) for row in led.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == before
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
    original = worker._CloudProvider
    class Paid(original):
        def __call__(self, prompt, **kwargs):
            sent.append(prompt)
            self.completed_reservations = [object()]   # the attempt was billed
            raise worker.CloudFallback("synthetic_failure")
    monkeypatch.setattr(worker, "_CloudProvider", Paid)
    for _ in range(3):   # the rotation reaches the scan (lane B) after the fact-less summary
        result = step(runtime, local_admit=lambda: False)
        if result["status"] == "failed":
            break
    assert result["status"] == "failed" and result["reason"] == "nightly_cloud_failed" and result["lane"] == "B"
    assert not local_calls and len(sent) == 1
    stored = document(led, context_refresh.SCAN)
    assert stored["failure"] == "nightly_cloud_failed" and stored["attempts"] == 1 and stored["next_try"] > 0
    # the retry is recorded under the local route, where the next selection finds it
    meta = json.loads(led.db.execute("SELECT meta FROM artifacts WHERE kind=?", (context_refresh.SCAN,)).fetchone()[0])
    assert "provider" not in meta and meta["model"] == "synthetic"
    assert meta["route_contract"] == context_refresh._contract(CFG, 1) and "provider" not in meta["contract"]
    # the backoff keeps the next tick from re-sending the same unit
    assert step(runtime, local_admit=lambda: False)["status"] != "failed" and len(sent) == 1


def test_paid_failure_of_a_partial_cloud_scan_keeps_its_cloud_pin(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    snapshot = context_refresh._snapshot(led.db, 1, CFG)
    meta = {"source_sig": context_refresh._source_sig(snapshot["sources"][0]),
            "contract": snapshot["contract"], "provider": snapshot["contract"]["provider"]}
    with led.db:
        context_refresh._replace(led, context_refresh.SCAN, 1,
                                 {"cursor": 1, "chunks": 2, "complete": False, "facts": []}, meta, 1)
    sent = []
    original = worker._CloudProvider
    class Paid(original):
        def __call__(self, prompt, **kwargs):
            sent.append(prompt)
            self.completed_reservations = [object()]   # the attempt was billed
            raise worker.CloudFallback("synthetic_failure")
    monkeypatch.setattr(worker, "_CloudProvider", Paid)
    for _ in range(3):
        result = step(runtime, local_admit=lambda: False)
        if result["status"] == "failed":
            break
    assert result["status"] == "failed" and result["reason"] == "nightly_cloud_failed"
    assert not local_calls and len(sent) == 1
    # the retry stays on the cloud backend: no local record of a cloud cursor
    scan = context_refresh._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])[1]
    assert scan["_provider_contract"] == snapshot["contract"]["provider"]
    assert scan["failure"] == "nightly_cloud_failed" and scan["next_try"] > 0


def test_shadow_unit_stays_pending_when_the_run_lock_is_busy_after_measuring(runtime, cloud, monkeypatch):
    led, clock, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[1][0] = "shadow"
    measured = [False]
    original = worker._CloudProvider
    class Measured(original):
        def __call__(self, prompt, **kwargs):
            value = super().__call__(prompt, **kwargs)
            measured[0] = True
            clock.mono += 600          # the lock wait runs into the unit deadline
            return value
    monkeypatch.setattr(worker, "_CloudProvider", Measured)
    monkeypatch.setattr(context_refresh, "acquire_run_lock",
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
