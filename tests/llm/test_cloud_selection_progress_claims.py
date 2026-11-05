"""Synthetic cloud selection progress claims contracts with isolated shared fixtures."""
import extract_llm_carry
import extract_llm_context
import extract_llm_engine
import extract_llm_errors
import extract_llm_prepare
import extract_llm_progress
import extract_llm_provider
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
import extraction_context
from contextlib import closing
from decimal import Decimal
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
import pytest
import cloud_policy
import extract_llm
import cloud_inference
from anthropic_client import AnthropicClient
from ledger import Ledger
from test_anthropic_runtime import _assert_legacy_retained as _assert_legacy_retained, _current_artifacts as _current_artifacts, old as old, routed as routed
from cloud_selection_progress_testkit import _REAL_BLOCK as _REAL_BLOCK, _REAL_RESERVER as _REAL_RESERVER, _REAL_RESULT as _REAL_RESULT, _claim_count as _claim_count, _cloud_pass as _cloud_pass, _complete_cloud_tx as _complete_cloud_tx, _complete_local_tx as _complete_local_tx, _select as _select, blocked_prefix as blocked_prefix, cloud_candidates as cloud_candidates


def test_isolated_receipt_snapshot_still_requires_fresh_authority_before_preparation(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    ready, revoked = cloud_policy.history_target_ready, []

    def revoke(db, pid, mid):
        allowed = ready(db, pid, mid)
        if db is not led.db and not revoked:
            led.artifact_add("cloud_history_scope", '{"version":1,"sources":{}}')
            revoked.append(True)
        return allowed

    monkeypatch.setattr(cloud_policy, "history_target_ready", revoke)
    report = _cloud_pass(led)
    assert revoked == [True] and report["selected"] == report["context_blocked"] == 64
    assert report["done"] == 0 and not led.artifacts(extraction_context.KIND)
    assert calls == reservations == results == [] and _claim_count(led) == 0


@pytest.mark.parametrize("blocked_count", [0, 3])
def test_cloud_prepares_only_through_the_successful_claim(cloud_candidates, monkeypatch, blocked_count):
    led, _, calls, reservations, results = cloud_candidates
    selected = _select(led, "extract")
    prepare = extract_llm._prepare_jobs
    prepared = []

    def one_candidate(ledger, rows, *args, **kwargs):
        prepared.extend(row["message_id"] for row in rows)
        if len(prepared) <= blocked_count:
            return [], 0, 1
        return prepare(ledger, rows, *args, **kwargs)

    monkeypatch.setattr(extract_llm, "_prepare_jobs", one_candidate)
    monkeypatch.setattr(extract_llm_runner, '_prepare_jobs', extract_llm._prepare_jobs)
    report = _cloud_pass(led)
    assert report["done"] == 1 and report["selected"] == 64
    assert report["context_blocked"] == report["deferred"] == blocked_count
    assert report["defer_reasons"] == ({"prepare_blocked": blocked_count} if blocked_count else {})
    assert prepared == [row["message_id"] for row in selected[:blocked_count + 1]]
    assert [row["message_id"] for row in led.artifacts(extraction_context.KIND)] == [
        selected[blocked_count]["message_id"]]
    assert len(calls) == len(reservations) == len(results) == 1 and _claim_count(led) == 0


@pytest.mark.parametrize("expires_after", ["selection", "preparation"])
def test_cloud_deadline_stops_preparation_and_claims(cloud_candidates, monkeypatch, expires_after):
    led, _, calls, reservations, results = cloud_candidates
    clock = [time.monotonic()]
    select, prepare = extract_llm._select_pending, extract_llm._prepare_jobs
    prepared = []

    def selected(*args, **kwargs):
        result = select(*args, **kwargs)
        if expires_after == "selection":
            clock[0] += 181
        return result

    def preparing(ledger, rows, *args, **kwargs):
        prepared.extend(row["message_id"] for row in rows)
        result = prepare(ledger, rows, *args, **kwargs)
        clock[0] += 181
        return result

    monkeypatch.setattr(extract_llm, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(extract_llm_carry, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_context, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_engine, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_errors, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_prepare, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_provider, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_resident, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_runner, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_selection, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_source_contract, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_store, 'time', extract_llm.time)
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm, "_select_pending", selected)
    monkeypatch.setattr(extract_llm_runner, '_select_pending', extract_llm._select_pending)
    monkeypatch.setattr(extract_llm, "_prepare_jobs", preparing)
    monkeypatch.setattr(extract_llm_runner, '_prepare_jobs', extract_llm._prepare_jobs)
    monkeypatch.setattr(extract_llm, "_claim", lambda *a: pytest.fail("expired pass claimed a source"))
    monkeypatch.setattr(extract_llm_runner, '_claim', extract_llm._claim)
    report = _cloud_pass(led)
    assert report["done"] == 0
    assert report["deferred"] == int(expires_after == "preparation")
    assert report["defer_reasons"] == ({"deadline": 1} if expires_after == "preparation" else {})
    assert len(prepared) == int(expires_after == "preparation")
    assert calls == reservations == results == [] and _claim_count(led) == 0


def test_deadline_after_claim_before_inference_keeps_reason(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    clock = [time.monotonic()]
    plan = extract_llm._plan_units

    def expire_before_workers(*args, **kwargs):
        units = plan(*args, **kwargs)
        clock[0] += 181
        return units

    monkeypatch.setattr(extract_llm, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(extract_llm_carry, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_context, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_engine, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_errors, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_prepare, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_provider, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_resident, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_runner, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_selection, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_source_contract, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_store, 'time', extract_llm.time)
    monkeypatch.setattr(cloud_inference, "time", extract_llm.time)
    monkeypatch.setattr(extract_llm, "_plan_units", expire_before_workers)
    monkeypatch.setattr(extract_llm_runner, '_plan_units', extract_llm._plan_units)
    report = _cloud_pass(led)
    assert report["deferred"] == 1 and report["defer_reasons"] == {"deadline": 1}
    assert calls == reservations == results == [] and _claim_count(led) == 0


def test_empty_cloud_pass_does_not_count_the_global_backlog(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    monkeypatch.setattr(extract_llm, "_backlog_report",
                        lambda *a: pytest.fail("cloud pass counted the global backlog"))
    monkeypatch.setattr(extract_llm_runner, '_backlog_report', extract_llm._backlog_report)
    for _ in range(2):
        report = extract_llm.run_pending(led, limit=1, budget_s=180, admitted_ids=set(),
            background=True, cloud_only=True, prioritize_patients=True)
        assert report["done"] == report["selected"] == 0
        assert report["left"] is report["left_estimate"] is report["left_estimate_age_s"] is None
        assert report["queue_ages_s"] == {"oldest": None, "newest": None}
    assert len(led.artifacts(extract_llm.KIND)) == 64
    assert calls == reservations == results == [] and _claim_count(led) == 0


def test_two_connections_refill_a_claim_lost_after_selection(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    selected = _select(led, "extract")
    path = led.db.execute("PRAGMA database_list").fetchone()[2]
    claim, release = extract_llm._claim, extract_llm._release
    competing = []
    with closing(Ledger(path)) as other:
        assert [r["message_id"] for r in _select(other, "extract")] == [r["message_id"] for r in selected]

        def race(ledger, row, lease_s):
            if ledger is led and not competing:
                foreign = dict(row)
                lease = claim(other, foreign, lease_s)
                assert lease is not None
                competing.append((foreign, lease))
            return claim(ledger, row, lease_s)

        monkeypatch.setattr(extract_llm, "_claim", race)
        monkeypatch.setattr(extract_llm_runner, '_claim', extract_llm._claim)
        try:
            first = _cloud_pass(led)
            assert first["done"] == 1 and first["selected"] == 64
            assert [r["message_id"] for r in _current_artifacts(led)] == [selected[1]["message_id"]]
            assert _claim_count(led) == 1  # The competing owner's lease remains intact.
            assert {r["message_id"] for r in led.artifacts(extraction_context.KIND)} == {
                row["message_id"] for row in selected[:2]}
        finally:
            if competing:
                release(other, *competing[0])
        assert _cloud_pass(other)["done"] == 1
    assert len(calls) == len(reservations) == len(results) == 2
    assert len({r["identity"]["message_id"] for r in reservations}) == 2
    assert len(_current_artifacts(led)) == 2 and _claim_count(led) == 0


@pytest.mark.parametrize("change", ["completed", "local", "retry"])
def test_cloud_claim_rechecks_pending_after_another_worker_changes_it(cloud_candidates, monkeypatch, change):
    led, _, calls, reservations, results = cloud_candidates
    selected = _select(led, "extract")
    first = selected[0]["message_id"]
    original_claim = extract_llm._claim
    changed = []

    def race(ledger, row, lease_s):
        if not changed:
            assert row["message_id"] == first
            meta = {"hash": row["content_hash"], "extract_version": extract_llm.EXTRACT_VERSION,
                    extract_llm.inference.VERSION_KEY: extract_llm._INFERENCE_VERSION}
            if change == "retry":
                meta.update(error=1, attempts=1, next_try=0)
            if change in ('completed', 'local'):
                with ledger.db:
                    ledger.db.execute('BEGIN IMMEDIATE')
                    (_complete_cloud_tx if change == 'completed' else _complete_local_tx)(ledger, row)
            else:
                ledger.artifact_add(extract_llm.KIND, '{"symptoms":[]}',
                                    project_id=row["project_id"], message_id=first, meta=meta)
            changed.append(first)
        return original_claim(ledger, row, lease_s)

    monkeypatch.setattr(extract_llm, "_claim", race)
    monkeypatch.setattr(extract_llm_runner, '_claim', extract_llm._claim)
    report = _cloud_pass(led)
    assert changed == [first] and report["done"] == 1
    assert len(calls) == len(reservations) == len(results) == 1
    assert reservations[0]["identity"]["message_id"] == (
        first if change == 'local' else selected[1]["message_id"])
    assert _claim_count(led) == 0


@pytest.mark.parametrize("change", ["completed", "retry"])
def test_background_cloud_claim_rechecks_pending_without_cloud_only(cloud_candidates, monkeypatch, change):
    led, _, calls, reservations, results = cloud_candidates
    original_claim = extract_llm._claim
    changed = []

    def race(ledger, row, lease_s):
        if not changed:
            meta = {"hash": row["content_hash"], "extract_version": extract_llm.EXTRACT_VERSION,
                    extract_llm.inference.VERSION_KEY: extract_llm._INFERENCE_VERSION}
            if change == "retry":
                meta.update(error=1, attempts=1, next_try=0)
            ledger.artifact_add(extract_llm.KIND, '{"symptoms":[]}', project_id=row["project_id"],
                                message_id=row["message_id"], meta=meta)
            changed.append(row["message_id"])
        return original_claim(ledger, row, lease_s)

    monkeypatch.setattr(extract_llm, "_claim", race)
    monkeypatch.setattr(extract_llm_runner, '_claim', extract_llm._claim)
    first = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
                                    allow_cloud=True, prioritize_patients=True)
    assert changed and first["done"] == 0
    assert calls == reservations == results == [] and _claim_count(led) == 0
    if change == "retry":
        assert led.db.execute(f"SELECT 1 FROM messages m WHERE m.message_id=? AND {extract_llm.pending_pred(led.db)}",
                              (changed[0],)).fetchone()
    else:
        second = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
                                         allow_cloud=True, prioritize_patients=True)
        assert second["done"] == 1 and len(reservations) == len(calls) == len(results) == 1
        assert reservations[0]["identity"]["message_id"] != changed[0]
    assert _claim_count(led) == 0


def test_sparse_single_claim_keeps_serial_checkpoint_and_batch_boundaries(cloud_candidates, monkeypatch):
    import concurrent.futures
    led, _, calls, reservations, results = cloud_candidates
    selected = _select(led, "extract")
    chosen = selected[-1]["message_id"]
    claim, capture = extract_llm._claim, extract_llm._capture_checkpoint_state
    owner = threading.get_ident()
    captured = []

    def last_claim(ledger, row, lease_s):
        return claim(ledger, row, lease_s) if row["message_id"] == chosen else None

    def after_claim(ledger, row, context):
        assert threading.get_ident() == owner and row["_claim_token"]
        captured.append(row["message_id"])
        return capture(ledger, row, context)

    monkeypatch.setattr(extract_llm, "_claim", last_claim)
    monkeypatch.setattr(extract_llm_runner, '_claim', extract_llm._claim)
    monkeypatch.setattr(extract_llm, "_capture_checkpoint_state", after_claim)
    monkeypatch.setattr(extract_llm_runner, '_capture_checkpoint_state', extract_llm._capture_checkpoint_state)
    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor",
                        lambda **kw: pytest.fail("one claimed unit created a thread pool"))
    report = _cloud_pass(led)
    assert report["done"] == 1 and report["selected"] == 64 and captured == [chosen]
    assert len(calls) == len(reservations) == len(results) == report["llm_calls"]["calls"] == 1
    assert [r["message_id"] for r in _current_artifacts(led)] == [chosen]
    assert _claim_count(led) == 0
    def item(index, mode):
        row = {"_cloud_mode": mode, "_chunk_size": extract_llm._CHUNK_SIZE,
               "body_text": "synthetic", "thin_src": None}
        return index, row, None, {}, {}, None, 1
    units = extract_llm._plan_units([item(1, "off"), item(2, "off"), item(3, "on"),
                                     item(4, "off"), item(5, "off")], 2)
    assert [(kind, [t[0] for t in payload] if kind == "batch" else payload[0])
            for kind, payload in units] == [("batch", [1, 2]), ("single", 3), ("batch", [4, 5])]


def test_after_claim_checkpoint_replays_without_another_cloud_call(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    claim = extract_llm._claim
    inserted = []

    def checkpoint_after_claim(ledger, row, lease_s):
        lease = claim(ledger, row, lease_s)
        if lease is not None:
            row["_chunk_size"] = extract_llm._saved_chunk_size(ledger.db, row, row["_context"])
            assert not extract_llm._saved_chunk_state(ledger.db, row, row["_context"],
                chunk_size=row["_chunk_size"])[1]
            value = extract_llm.extract_validation.validate(
                {"symptoms": [], "summary": row["body_text"]}, row["body_text"])
            assert value is not None
            value["_quality_result"] = {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}
            leaf = extract_llm._EvidenceLeaf(value, extract_llm._provider_evidence(["anthropic"], True))
            assert extract_llm._persist_chunks(ledger, row, {0: leaf}, row["_context"],
                chunk_size=row["_chunk_size"], lease=lease)
            inserted.append(row["message_id"])
        return lease

    monkeypatch.setattr(extract_llm, "_claim", checkpoint_after_claim)
    monkeypatch.setattr(extract_llm_runner, '_claim', extract_llm._claim)
    report = _cloud_pass(led)
    assert report["done"] == 1 and len(inserted) == 1
    assert report["llm_calls"]["checkpoint_reuses"] == 1
    assert calls == reservations == results == []
    current = _current_artifacts(led)
    assert [r["message_id"] for r in current] == inserted and _claim_count(led) == 0
    assert json.loads(current[0]["meta"])["backend"] == "anthropic"


@pytest.mark.parametrize("failure", [None, "preparation", "checkpoint", "rules"])
def test_claim_limit_and_after_claim_failure_release_owned_leases(cloud_candidates, monkeypatch, failure):
    led, _, calls, reservations, results = cloud_candidates
    claim, capture, ensure = extract_llm._claim, extract_llm._capture_checkpoint_state, extract_llm._ensure_v1
    prepare = extract_llm._prepare_jobs
    owned = []

    def preparing(*args, **kwargs):
        if failure == "preparation" and owned:
            raise RuntimeError("synthetic-after-claim")
        return prepare(*args, **kwargs)

    def owned_claim(ledger, row, lease_s):
        lease = claim(ledger, row, lease_s)
        if lease is not None:
            owned.append(row["message_id"])
        return lease

    def after_claim(ledger, row, context):
        if failure == "checkpoint" and len(owned) == 2:
            raise RuntimeError("synthetic-after-claim")
        return capture(ledger, row, context)

    def rules(ledger, row, hints):
        if failure == "rules" and len(owned) == 2:
            raise RuntimeError("synthetic-after-claim")
        return ensure(ledger, row, hints)

    monkeypatch.setattr(extract_llm, "_claim", owned_claim)
    monkeypatch.setattr(extract_llm_runner, '_claim', extract_llm._claim)
    monkeypatch.setattr(extract_llm, "_prepare_jobs", preparing)
    monkeypatch.setattr(extract_llm_runner, '_prepare_jobs', extract_llm._prepare_jobs)
    monkeypatch.setattr(extract_llm, "_capture_checkpoint_state", after_claim)
    monkeypatch.setattr(extract_llm_runner, '_capture_checkpoint_state', extract_llm._capture_checkpoint_state)
    monkeypatch.setattr(extract_llm, "_ensure_v1", rules)
    monkeypatch.setattr(extract_llm_prepare, '_ensure_v1', extract_llm._ensure_v1)
    monkeypatch.setattr(extract_llm_runner, '_ensure_v1', extract_llm._ensure_v1)
    if failure is None:
        assert _cloud_pass(led, limit=2)["done"] == 2
        assert len(calls) == len(reservations) == len(results) == len(_current_artifacts(led)) == 2
        assert len(led.artifacts(extraction_context.KIND)) == 2
    else:
        with pytest.raises(RuntimeError, match="synthetic-after-claim"):
            _cloud_pass(led, limit=2)
        assert calls == reservations == results == [] and not _current_artifacts(led)
    assert len(owned) == (1 if failure == "preparation" else 2)
    assert _claim_count(led) == 0 and not led.db.in_transaction


@pytest.mark.parametrize("change", ["scope", "source", "budget", "unknown_paid"])
def test_claim_refill_preserves_live_send_and_unresolved_charge_guards(cloud_candidates, monkeypatch, change):
    led, cfg, *_ = cloud_candidates
    prior = [dict(row) for row in led.artifacts(extract_llm.KIND)]
    monkeypatch.setattr(cloud_policy, "block_reason", _REAL_BLOCK)
    monkeypatch.setattr(cloud_policy, "record_result", _REAL_RESULT)
    monkeypatch.setattr(extract_llm.anthropic_client, "AnthropicClient", AnthropicClient)
    sent, unresolved, changed = [], [], []

    def forbidden_http(*args, **kwargs):
        sent.append(True)
        pytest.fail("blocked cloud unit reached HTTP")

    def reserve_factory(*args, **kwargs):
        reserve = _REAL_RESERVER(*args, **kwargs)
        def reserve_once(body, timeout):
            if change == "unknown_paid":
                unresolved.append(reserve(body, timeout))
            return reserve(body, timeout)
        return reserve_once

    ensure = extract_llm._ensure_v1
    def change_after_claim(ledger, row, hints):
        ensure(ledger, row, hints)
        changed.append(row["message_id"])
        if change == "scope":
            cfg["cloud_llm"]["exclude_project_ids"] = [1]
        elif change == "source":
            with ledger.db:
                ledger.db.execute("UPDATE messages SET content_hash='changed' WHERE message_id=?",
                                   (row["message_id"],))
        elif change == "budget":
            cfg["cloud_llm"]["daily_request_budget"] = 0

    monkeypatch.setattr(AnthropicClient, "request", forbidden_http)
    monkeypatch.setattr(cloud_policy, "usage_reserver", reserve_factory)
    monkeypatch.setattr(extract_llm, "_ensure_v1", change_after_claim)
    monkeypatch.setattr(extract_llm_prepare, '_ensure_v1', extract_llm._ensure_v1)
    monkeypatch.setattr(extract_llm_runner, '_ensure_v1', extract_llm._ensure_v1)
    assert _cloud_pass(led)["done"] == 0 and len(changed) == 1
    assert sent == [] and _claim_count(led) == int(change == "unknown_paid") and not _current_artifacts(led)
    _assert_legacy_retained(led, prior)
    usage = cloud_policy.usage_report(led)
    assert not usage["pricing_unverified"]
    assert usage["unresolved_requests"] == len(unresolved) == int(change == "unknown_paid")
    if unresolved:
        assert usage["daily_requests"] == 1 and Decimal(usage["max_reserved_cost_usd"]) > 0
        assert not led.artifacts("cloud_usage_result")
    else:
        assert usage["daily_requests"] == 0


@pytest.mark.parametrize('updater_changed', [False, True])
def test_cloud_claim_preparation_exception_preserves_claim_only_after_observed_update(
        cloud_candidates, monkeypatch, updater_changed):
    led, cfg, calls, reservations, results = cloud_candidates
    cfg['cloud_llm']['max_concurrency'] = 1
    state, releases = [(False, 1)], []
    original_release = extract_llm._release
    def release(*args):
        releases.append(True)
        return original_release(*args)
    def fail(*args):
        if updater_changed:
            state[0] = (False, 2)
        raise sqlite3.OperationalError('synthetic preparation exception')
    monkeypatch.setattr(extract_llm.writer_lock, 'updater_state', lambda: state[0])
    monkeypatch.setattr(extract_llm, '_release', release)
    monkeypatch.setattr(extract_llm_carry, '_release', extract_llm._release)
    monkeypatch.setattr(extract_llm_runner, '_release', extract_llm._release)
    monkeypatch.setattr(extract_llm, '_ensure_v1', fail)
    monkeypatch.setattr(extract_llm_prepare, '_ensure_v1', extract_llm._ensure_v1)
    monkeypatch.setattr(extract_llm_runner, '_ensure_v1', extract_llm._ensure_v1)
    with pytest.raises(sqlite3.OperationalError, match='synthetic preparation exception'):
        extract_llm.run_pending(led, limit=1, workers=1, budget_s=180,
            background=True, cloud_only=True, prioritize_patients=True, carry=[])
    assert _claim_count(led) == int(updater_changed)
    assert len(releases) == int(not updater_changed)
    assert not _current_artifacts(led) and calls == reservations == results == []


@pytest.mark.parametrize("detail,retained", [("cloud_unit_unresolved", True),
                                            ("deadline", False),
                                            ("synthetic-private", False)])
def test_refused_unit_yields_selection_without_bypassing_send_guard(cloud_candidates, monkeypatch, detail, retained):
    led, _, calls, reservations, results = cloud_candidates
    first = _select(led, "extract")[0]["message_id"]
    original = extract_llm._llm_call
    def refuse(*args, **kwargs):
        raise extract_llm.cloud_client.CloudNotSent("budget_exceeded", detail)
    monkeypatch.setattr(extract_llm, "_llm_call", refuse)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    report = _cloud_pass(led)
    expected = extract_llm.cloud_client.not_sent_reason(
        extract_llm.cloud_client.CloudNotSent("budget_exceeded", detail))
    assert report['deferred'] == 1 and report['defer_reasons'] == {expected: 1}
    assert not calls and not reservations and not results
    assert _claim_count(led) == int(retained)
    if retained:
        assert first not in [r['message_id'] for r in _select(led, "extract")]
        monkeypatch.setattr(extract_llm, "_llm_call", original)
        monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
        monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
        assert _cloud_pass(led)['done'] == 1
        assert first not in [r['message_id'] for r in _current_artifacts(led)]
        with led.db:
            led.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='extract_claim'")
        assert first in [r['message_id'] for r in _select(led, "extract")]
        monkeypatch.setattr(extract_llm, "_llm_call", refuse)
        monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
        monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
        # Expiry permits a fresh check, not a bypass of the unresolved-send guard.
        assert _cloud_pass(led)['deferred'] == 1
        assert len(calls) == len(reservations) == len(results) == 1


@pytest.mark.parametrize("cause,expected", [
    ("deadline", "prepare_deadline"),
    ("source", "prepare_source_changed"),
    ("cloud_disabled", "prepare_cloud_disabled"),
    ("profile", "prepare_profile_unavailable"),
    ("medication", "prepare_medication_changed"),
    ("context", "prepare_context_other"),
    ("database", "prepare_database_unavailable"),
])
def test_prepare_refusal_reports_fixed_reason_without_private_error(cloud_candidates, monkeypatch, cause, expected):
    led, *_ = cloud_candidates
    rows, _ = extract_llm._select_pending(led, 1, None, None, False, cloud_only=True)
    assert len(rows) == 1
    rows = [dict(rows[0])]
    deadline = time.monotonic() + 60
    if cause == "deadline":
        deadline = time.monotonic() - 1
    elif cause == "source":
        rows[0]["_source_binding"] = "synthetic-stale-binding"
    elif cause == "cloud_disabled":
        monkeypatch.setattr(extract_llm, "_cloud_mode", lambda *a: "off")
        monkeypatch.setattr(extract_llm_prepare, '_cloud_mode', extract_llm._cloud_mode)
    elif cause == "profile":
        original_profile = cloud_policy.resolve_profile
        calls = []
        def changed_profile(*a, **kw):
            calls.append(True)
            # Admit first, then fail while freezing the prepared job's profile.
            return original_profile(*a, **kw) if len(calls) == 1 else (None, ["private-detail"])
        monkeypatch.setattr(cloud_policy, "resolve_profile", changed_profile)
    elif cause == "medication":
        monkeypatch.setattr(extract_llm, "_medication_reference_current", lambda *a, **kw: False)
        monkeypatch.setattr(extract_llm_context, '_medication_reference_current', extract_llm._medication_reference_current)
        monkeypatch.setattr(extract_llm_prepare, '_medication_reference_current', extract_llm._medication_reference_current)
        monkeypatch.setattr(extract_llm_progress, '_medication_reference_current', extract_llm._medication_reference_current)
        monkeypatch.setattr(extract_llm_store, '_medication_reference_current', extract_llm._medication_reference_current)
    elif cause in ("context", "database"):
        def unavailable(*args, **kwargs):
            import sqlite3
            error = sqlite3.OperationalError if cause == "database" else ValueError
            raise error("private-detail")
        monkeypatch.setattr(extract_llm, "_pending_context_frame", unavailable)
        monkeypatch.setattr(extract_llm_prepare, '_pending_context_frame', extract_llm._pending_context_frame)
    reasons = {}
    jobs, skipped, blocked = extract_llm._prepare_jobs(
        led, rows, extract_llm._resolved_llm(), False, cloud_only=True,
        load_checkpoints=False, deadline=deadline, blocked_reasons=reasons)
    assert jobs == [] and skipped == 0 and blocked == 1
    assert reasons == {expected: 1}
    assert expected in extract_llm.cloud_backlog._DEFER_REASONS
    assert _claim_count(led) == 0


def test_prepare_reasons_reach_cloud_pass_accounting(cloud_candidates, monkeypatch):
    led, *_ = cloud_candidates
    original = extract_llm._prepare_jobs
    rejected = []
    def changed_once(ledger, rows, *args, **kwargs):
        if not rejected:
            rejected.append(rows[0]["message_id"])
            rows = [{**rows[0], "_source_binding": "synthetic-stale-binding"}]
        return original(ledger, rows, *args, **kwargs)
    monkeypatch.setattr(extract_llm, "_prepare_jobs", changed_once)
    monkeypatch.setattr(extract_llm_runner, '_prepare_jobs', extract_llm._prepare_jobs)
    report = _cloud_pass(led)
    assert report["done"] == 1 and report["context_blocked"] == 1
    assert report["defer_reasons"] == {"prepare_source_changed": 1}
