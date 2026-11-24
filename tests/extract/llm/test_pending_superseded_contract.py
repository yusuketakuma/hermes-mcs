"""Chunk records written under a retired contract generation classify as
superseded — never resumed, never silently unverifiable (fully synthetic)."""
import hashlib
import json
import time
from unittest.mock import patch

import pytest

import anthropic_client
import cloud_client
import cloud_inference
import cloud_policy
from clinical_chunking import plan_chunks
import extract_llm as worker
import extract_llm_context
import extract_llm_engine
import extract_llm_historical
import extract_llm_provider
import extract_llm_runner
import extraction_context as frames
import mcs_queries
from anthropic_runtime_testkit import _use_real_cloud_accounting
from inference_dependency_loss import source_binding
from inference_version import CURRENT_VERSION, VERSION_KEY
from test_anthropic_runtime import old, routed
from test_cloud_response_recovery import legacy_cloud_routes

__all__ = ['routed']

ATTEMPT = {'qc_src': None, 'thin_src': None, 'human_src': None, 'auto_retry': 0}


def _frozen(ledger, body=None, mid=1):
    old(ledger, mid=mid)
    if body is not None:
        digest = hashlib.sha256(body.encode()).hexdigest()
        with ledger.db:
            ledger.db.execute(
                "UPDATE messages SET body_text=?, content_hash=? WHERE message_id=?",
                (body, digest, mid))
    row = dict(ledger.db.execute(
        'SELECT * FROM messages WHERE message_id=?', (mid,)).fetchone())
    row.update(_source_binding=source_binding(ledger.db, row['project_id'], mid),
               _target=('', 'synthetic-model'), _cloud_mode='off')
    frame = frames.prepare(ledger, row, model='synthetic-model',
        contract=worker._context_contract(), attempt=ATTEMPT, enabled=False,
        base_context=None, version=worker.EXTRACT_VERSION)
    return row, frame


def _persist(ledger, row, frame, context, model='synthetic-model'):
    return ledger.artifact_add('extract_llm_chunk', '{}',
        project_id=row['project_id'], message_id=row['message_id'], model=model,
        meta={VERSION_KEY: CURRENT_VERSION, 'hash': row['content_hash'],
              'ver': worker.EXTRACT_VERSION, 'chunk': 0, 'context': context,
              'chunk_size': worker._CHUNK_SIZE, 'split_size': worker._CHUNK_SIZE // 3,
              'piece_sha256': 'a' * 64, 'actual_model': model,
              'plan_version': worker.PLAN_VERSION})


def _context_for(row, frame, model, context):
    bound = {**row, '_target': ('', model), '_frame': frame}
    if context == 'current':
        return worker._chunk_context(bound, frame['text'], checkpoint=True)
    _gen_id, prompt_head, schema = extract_llm_historical.known_old_contracts()[context]
    return worker._chunk_context(bound, frame['text'], checkpoint=True,
                                 prompt_head=prompt_head, schema=schema)


def _current_context(row, frame, model='synthetic-model'):
    return _context_for(row, frame, model, 'current')


def _old_context(row, frame, index=0, model='synthetic-model'):
    return _context_for(row, frame, model, index)


def test_retired_generation_checkpoint_is_superseded(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    with pytest.raises(ValueError, match='pending_context_superseded'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)


def test_second_retired_generation_checkpoint_is_superseded(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame, index=1))
    with pytest.raises(ValueError, match='pending_context_superseded'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)


def test_unknown_checkpoint_stays_unverifiable(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, 'f' * 64)
    with pytest.raises(ValueError, match='pending_context_unverifiable'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)


def test_partially_recognized_records_stay_unverifiable(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    _persist(ledger, row, frame, 'f' * 64)
    with pytest.raises(ValueError, match='pending_context_unverifiable'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)


def test_current_checkpoint_still_resumes(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _current_context(row, frame))
    picked = worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)
    assert picked is not None and picked['artifact_id'] == frame['artifact_id']


def test_mixed_current_and_retired_records_stay_unverifiable(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _current_context(row, frame))
    _persist(ledger, row, frame, _old_context(row, frame))
    with pytest.raises(ValueError, match='pending_context_unverifiable'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)


def _scan_row(ledger, row):
    return {**row, '_source_binding': source_binding(ledger.db, row['project_id'], row['message_id'])}


def test_scan_flags_fully_retired_pending_set(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    assert extract_llm_context._pending_superseded_scan(ledger.db, _scan_row(ledger, row)) is True


def test_scan_sees_retired_records_under_a_different_model(routed):
    ledger, *_ = routed
    row, _frame = _frozen(ledger)
    cloud_row = dict(row)
    cloud_row['_target'] = ('', 'retired-cloud-model')
    cloud_frame = frames.prepare(ledger, cloud_row, model='retired-cloud-model',
        contract=worker._context_contract(), attempt=ATTEMPT, enabled=False,
        base_context=None, version=worker.EXTRACT_VERSION)
    _persist(ledger, row, cloud_frame, _old_context(row, cloud_frame, model='retired-cloud-model'),
             model='retired-cloud-model')
    scan_row = _scan_row(ledger, row)
    scan_row['_target'] = ('', 'synthetic-model')
    assert extract_llm_context._pending_superseded_scan(ledger.db, scan_row) is True


def test_scan_rejects_current_or_mixed_or_unknown(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    scan_row = _scan_row(ledger, row)
    assert extract_llm_context._pending_superseded_scan(ledger.db, scan_row) is False
    _persist(ledger, row, frame, _current_context(row, frame))
    assert extract_llm_context._pending_superseded_scan(ledger.db, scan_row) is False
    _persist(ledger, row, frame, _old_context(row, frame))
    assert extract_llm_context._pending_superseded_scan(ledger.db, scan_row) is False


def _prepare(ledger, rows):
    reasons = {}
    jobs, skipped, blocked = worker._prepare_jobs(
        ledger, rows, ('', 'synthetic-model'), False, load_checkpoints=False,
        blocked_reasons=reasons)
    return jobs, skipped, blocked, reasons


def _pending_rows(ledger):
    rows, _fresh = worker._select_pending(ledger, 1, None, None, False)
    return rows


def test_prepare_migrates_fully_retired_pending_set(routed):
    """Clean paid history -> one current-contract local job, cloud state absent."""
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    jobs, _skipped, blocked, reasons = _prepare(ledger, _pending_rows(ledger))
    assert blocked == 0 and reasons == {}
    assert len(jobs) == 1
    r, ctx, _saved, _hints, _qc = jobs[0]
    assert r["_superseded_local"] is True and r["_cloud_mode"] == "off"
    assert "_cloud_selection" not in r and "_provider_contract" not in r
    # a fresh current-contract frame is persisted once; a second prepare
    # reuses the same immutable artifact instead of duplicating it
    assert r["_frame"]["artifact_id"] != frame["artifact_id"]
    assert ctx == r["_frame"]["text"]
    jobs2, _s2, _b2, _r2 = _prepare(ledger, _pending_rows(ledger))
    assert jobs2[0][0]["_frame"]["artifact_id"] == r["_frame"]["artifact_id"]


def test_multi_piece_superseded_body_stays_held(routed):
    """The migrated contract covers a single-leaf request: a body that
    plan_chunks splits keeps the superseded hold before any frame work."""
    ledger, cfg, *_ = routed
    body = "\n".join(f"合成の経過記録{i:03d}。症状は安定している。" for i in range(400))
    assert len(plan_chunks(body, worker._CHUNK_SIZE)) > 1
    row, frame = _frozen(ledger, body=body)
    _persist(ledger, row, frame, _old_context(row, frame))
    jobs, _skipped, blocked, reasons = _prepare(ledger, _pending_rows(ledger))
    assert jobs == [] and blocked == 1
    assert reasons == {'prepare_context_load_superseded': 1}


def _real_reserve(ledger, cfg, monkeypatch, prompt="完全合成の予約確認。"):
    _use_real_cloud_accounting(monkeypatch)
    cfg["cloud_llm"]["provider"] = "anthropic"
    selection, errors = cloud_policy.resolve_profile(
        cfg, route="reextract", purpose="backlog", root=worker.HOME)
    assert not errors and selection is not None
    reserve = cloud_policy.usage_reserver(
        ledger, cfg, route="reextract", project_id=1, message_id=1,
        unit_key="1", owner={}, selection=selection)
    # reextract sends are retired in the live policy; only the historical
    # reservation seed runs under the reopened route, like legacy_cloud_routes.
    client = anthropic_client.AnthropicClient(
        "synthetic-unused", model=selection["model"], effort=selection["effort"])
    with patch.object(cloud_policy, "send_route", lambda route: True):
        return reserve(client.body(prompt, selection["max_tokens"], None), 30)


def _guard(ledger, cfg, prompt):
    return cloud_inference.guard_reextract_local(
        ledger, cfg, prompt, project_id=1, message_id=1, unit_key="1",
        owner={}, input_binding=None)


def test_guard_admits_a_different_fingerprint_and_holds_same_or_absent(
        routed, monkeypatch):
    """The financial guard compares logical fingerprints, not ownership:
    a different valid fp passes while same/absent fp always holds."""
    ledger, cfg, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    _real_reserve(ledger, cfg, monkeypatch, prompt="完全合成の旧要求。")
    _guard(ledger, cfg, "完全合成の別の現在要求。")          # different fp: pass
    _guard(ledger, cfg, "完全合体の旧要求。")                # near-miss still differs: pass
    with pytest.raises(cloud_client.CloudError):
        _guard(ledger, cfg, "完全合成の旧要求。")            # same fp: hold
    # an absent fingerprint can never prove a different request
    with ledger.db:
        ledger.db.execute(
            "UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp') "
            "WHERE kind='cloud_usage_reserve'")
    with pytest.raises(cloud_client.CloudError):
        _guard(ledger, cfg, "完全合成の別の現在要求。")
    # a corrupt receipt can never prove a different request either
    with ledger.db:
        ledger.db.execute(
            "UPDATE artifacts SET meta='not-json' WHERE kind='cloud_usage_reserve'")
    with pytest.raises((cloud_client.CloudError, ValueError)):
        _guard(ledger, cfg, "完全合成の別の現在要求。")


def test_owned_paid_history_still_migrates_to_local_final(routed, monkeypatch):
    """Positive: an owned different-fp reservation stays untouched while the
    migrated row completes a current local final through the real guard."""
    ledger, cfg, calls, reserved, results = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    _real_reserve(ledger, cfg, monkeypatch, prompt="完全合成の旧cloud要求。")
    before = _artifact_rows(ledger, ("extract_llm_chunk", "extraction_context",
                                     "cloud_usage_reserve", "cloud_usage_result"))
    assert cloud_policy.owns_request(ledger.db, route="reextract",
                                     project_id=1, message_id=1)
    prompts = []

    def fake_call(prompt, **_kw):
        prompts.append(prompt)
        return {"symptoms": [], "summary": "合成の要約"}

    monkeypatch.setattr(extract_llm_engine, "_llm_call", fake_call)
    report = worker.run_pending(ledger, limit=1, budget_s=180)
    assert report["done"] == 1 and prompts
    assert calls == [] and reserved == [] and results == []
    # the old paid unit is preserved exactly: ownership, reservation rows,
    # and every pre-existing checkpoint/frame row are byte-for-byte unchanged
    assert cloud_policy.owns_request(ledger.db, route="reextract",
                                     project_id=1, message_id=1)
    after = _artifact_rows(ledger, ("extract_llm_chunk", "extraction_context",
                                    "cloud_usage_reserve", "cloud_usage_result"))
    for artifact_id, snapshot in before.items():
        assert after[artifact_id] == snapshot
    current = ledger.db.execute(
        "SELECT a.* FROM artifacts a JOIN messages m ON a.message_id=m.message_id "
        "WHERE a.kind=? " + mcs_queries.current_extract_pred()
        + " AND json_extract(a.meta,'$.extract_version')=?",
        (worker.KIND, worker.EXTRACT_VERSION)).fetchall()
    assert len(current) == 1
    assert json.loads(current[0]["content"])["summary"] == "合成の要約"


def test_same_fingerprint_history_holds_the_migration(routed, monkeypatch):
    """A reservation bound to the exact current prompt is the same request:
    the guard holds and no local dispatch or artifact happens."""
    ledger, cfg, _calls, _reserved, _results = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    prompts = []

    def capture(prompt, **_kw):
        prompts.append(prompt)
        return extract_llm_engine._DEFERRED

    monkeypatch.setattr(extract_llm_engine, "_llm_call", capture)
    first = worker.run_pending(ledger, limit=1, budget_s=180)
    assert first["done"] == 0 and len(prompts) == 1
    _real_reserve(ledger, cfg, monkeypatch, prompt=prompts[0])
    second = worker.run_pending(ledger, limit=1, budget_s=180)
    assert second["done"] == 0 and len(prompts) == 1   # guard fired before dispatch
    assert not ledger.db.execute(
        "SELECT 1 FROM artifacts a JOIN messages m ON a.message_id=m.message_id "
        "WHERE a.kind=? " + mcs_queries.current_extract_pred()
        + " AND json_extract(a.meta,'$.extract_version')=?",
        (worker.KIND, worker.EXTRACT_VERSION)).fetchall()


def _artifact_rows(ledger, kinds):
    marks = ",".join("?" * len(kinds))
    return {row["artifact_id"]: tuple(row) for row in ledger.db.execute(
        f"SELECT * FROM artifacts WHERE kind IN ({marks})", kinds)}


def test_retired_cohort_runs_local_and_is_adopted(routed, monkeypatch):
    """E2E: superseded cohort -> local-only execution -> readable current artifact."""
    ledger, _cfg_, calls, reserved, results = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    before = _artifact_rows(ledger, ("extract_llm_chunk", "extraction_context"))
    prompts = []

    def fake_call(prompt, **_kw):
        prompts.append(prompt)
        return {"symptoms": [], "summary": "合成の要約"}

    monkeypatch.setattr(extract_llm_engine, "_llm_call", fake_call)
    report = worker.run_pending(ledger, limit=1, budget_s=180)
    assert report["done"] == 1 and prompts
    # local-only: no cloud unit construction, no reservation/result accounting,
    # no provider HTTP — the routed cloud client saw zero calls.
    assert calls == [] and reserved == [] and results == []
    assert not ledger.artifacts("cloud_usage_reserve")
    assert not ledger.artifacts("cloud_usage_result")
    # the executed prompt is the current-contract clinical prompt
    assert worker._PROMPT_HEAD in prompts[0]
    # independent reader: exactly one current extract artifact, adopted content
    current = ledger.db.execute(
        "SELECT a.* FROM artifacts a JOIN messages m ON a.message_id=m.message_id "
        "WHERE a.kind=? " + mcs_queries.current_extract_pred()
        + " AND json_extract(a.meta,'$.extract_version')=?",
        (worker.KIND, worker.EXTRACT_VERSION)).fetchall()
    assert len(current) == 1
    assert json.loads(current[0]["content"])["summary"] == "合成の要約"
    # every pre-existing checkpoint/frame row is byte-for-byte unchanged
    after = _artifact_rows(ledger, ("extract_llm_chunk", "extraction_context"))
    for artifact_id, snapshot in before.items():
        assert after[artifact_id] == snapshot


def test_adoption_boundary_reverifies_and_rejects_a_late_hold(
        routed, monkeypatch):
    """A same-fingerprint reservation appearing after dispatch must stop the
    adoption inside the commit transaction — not just the next prepare."""
    ledger, _cfg, calls, reserved, results = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    before = _artifact_rows(ledger, ("extract_llm_chunk", "extraction_context"))
    real_guard = cloud_inference.guard_reextract_local
    guard_calls = []

    def late_hold(ledger_, cfg_, prompt, **kw):
        guard_calls.append(prompt)
        if len(guard_calls) >= 3:   # request guards ok; deferred adoption eval holds
            raise cloud_client.CloudError("budget_exceeded", "cloud_unit_unresolved")
        return real_guard(ledger_, cfg_, prompt, **kw)

    monkeypatch.setattr(cloud_inference, "guard_reextract_local", late_hold)
    prompts = []

    def fake_call(prompt, **_kw):
        prompts.append(prompt)
        return {"symptoms": [], "summary": "合成の要約"}

    monkeypatch.setattr(extract_llm_engine, "_llm_call", fake_call)
    report = worker.run_pending(ledger, limit=1, budget_s=180)
    # two request guards plus the deferred off-transaction adoption eval,
    # whose held verdict rejects without an in-transaction rescan
    assert report["done"] == 0 and len(prompts) == 1 and len(guard_calls) == 3
    assert not ledger.db.execute(
        "SELECT 1 FROM artifacts a JOIN messages m ON a.message_id=m.message_id "
        "WHERE a.kind=? " + mcs_queries.current_extract_pred()
        + " AND json_extract(a.meta,'$.extract_version')=?",
        (worker.KIND, worker.EXTRACT_VERSION)).fetchall()
    after = _artifact_rows(ledger, ("extract_llm_chunk", "extraction_context"))
    for artifact_id, snapshot in before.items():
        assert after[artifact_id] == snapshot
    # the held row stays pending: the next prepare's guard holds it again
    second = worker.run_pending(ledger, limit=1, budget_s=180)
    assert second["done"] == 0 and len(prompts) == 1


def test_guarded_prompt_is_the_dispatched_prompt(routed, monkeypatch):
    """The prompt the financial guard clears at prepare and re-verifies at
    adoption is byte-identical to the prompt the local call actually sends."""
    ledger, _cfg, _calls, _reserved, _results = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    seen = []
    real_frp = extract_llm_engine.first_request_prompt

    def spy(*args, **kwargs):
        prompt = real_frp(*args, **kwargs)
        seen.append(prompt)
        return prompt

    monkeypatch.setattr(extract_llm_engine, "first_request_prompt", spy)
    import extract_llm_runner
    monkeypatch.setattr(extract_llm_runner, "first_request_prompt", spy)
    prompts = []

    def fake_call(prompt, **_kw):
        prompts.append(prompt)
        return {"symptoms": [], "summary": "合成の要約"}

    monkeypatch.setattr(extract_llm_engine, "_llm_call", fake_call)
    report = worker.run_pending(ledger, limit=1, budget_s=180)
    assert report["done"] == 1 and len(prompts) == 1
    # prepare + adoption both composed the guarded candidate; it is the
    # exact string the engine dispatched
    assert len(seen) >= 2 and all(p == prompts[0] for p in seen)


def test_failed_local_attempt_preserves_every_old_row(routed, monkeypatch):
    """A failed migrated attempt deletes nothing: old checkpoints, frames,
    reservations and ownership stay byte-for-byte identical."""
    ledger, _cfg, _calls, _reserved, _results = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    before = _artifact_rows(ledger, ("extract_llm_chunk", "extraction_context",
                                     "cloud_usage_reserve", "cloud_usage_result"))

    def fake_call(prompt, **_kw):
        return None

    monkeypatch.setattr(extract_llm_engine, "_llm_call", fake_call)
    import extract_llm_runner
    monkeypatch.setattr(extract_llm_runner, "_llm_up", lambda deadline=None: True)
    report = worker.run_pending(ledger, limit=1, budget_s=180)
    assert report["done"] == 0
    after = _artifact_rows(ledger, ("extract_llm_chunk", "extraction_context",
                                    "cloud_usage_reserve", "cloud_usage_result"))
    for artifact_id, snapshot in before.items():
        assert after[artifact_id] == snapshot
    assert not ledger.db.execute(
        "SELECT 1 FROM artifacts a JOIN messages m ON a.message_id=m.message_id "
        "WHERE a.kind=? " + mcs_queries.current_extract_pred()
        + " AND json_extract(a.meta,'$.extract_version')=?",
        (worker.KIND, worker.EXTRACT_VERSION)).fetchall()


def test_parallel_superseded_jobs_complete_through_owner_guard(
        routed, monkeypatch):
    """Two migrated rows run concurrently: the financial guard's ledger
    reads marshal to the owner thread — no ProgrammingError masquerading
    as a permanent cloud_unit_unresolved."""
    ledger, _cfg, calls, reserved, results = routed
    prompts = []
    for mid, body in ((1, None), (2, "別の完全合成の記録。")):
        row, frame = _frozen(ledger, body=body, mid=mid)
        _persist(ledger, row, frame, _old_context(row, frame))

    def fake_call(prompt, **_kw):
        prompts.append(prompt)
        return {"symptoms": [], "summary": "合成の要約"}

    monkeypatch.setattr(extract_llm_engine, "_llm_call", fake_call)
    report = worker.run_pending(ledger, limit=2, budget_s=180, workers=2)
    assert report["done"] == 2 and len(prompts) == 2
    assert calls == [] and reserved == [] and results == []
    current = ledger.db.execute(
        "SELECT a.* FROM artifacts a JOIN messages m ON a.message_id=m.message_id "
        "WHERE a.kind=? " + mcs_queries.current_extract_pred()
        + " AND json_extract(a.meta,'$.extract_version')=?",
        (worker.KIND, worker.EXTRACT_VERSION)).fetchall()
    assert len(current) == 2


def _superseded_claim(ledger, row):
    return ledger.db.execute(
        "SELECT next_try FROM fetch_jobs WHERE kind='extract_claim' "
        "AND project_id=? AND message_id=?",
        (row["project_id"], row["message_id"])).fetchone()


def _multi_piece_body():
    return "\n".join(f"合成の経過記録{i:03d}。症状は安定している。" for i in range(400))


def test_superseded_hold_claim_waits_the_revive_interval(routed):
    """A still-superseded head keeps one bounded claim for the revive interval
    instead of re-blocking every pass; the row returns through the normal
    selection path only after that claim lapses."""
    ledger, *_ = routed
    row, frame = _frozen(ledger, body=_multi_piece_body())
    _persist(ledger, row, frame, _old_context(row, frame))
    report = worker.run_pending(ledger, limit=1, budget_s=180)
    assert report["defer_reasons"]["prepare_context_load_superseded"] == 1
    claim = _superseded_claim(ledger, row)
    assert claim and claim["next_try"] > time.time() + 5 * 3600


def test_corrected_post_is_not_hidden_by_the_superseded_hold(routed):
    """A post corrected during the hold is not stuck until the revive
    interval: the claim binds the claim-time content hash, so a different
    body is re-selected, re-evaluated once, and re-held under a new claim."""
    ledger, *_ = routed
    row, frame = _frozen(ledger, body=_multi_piece_body())
    _persist(ledger, row, frame, _old_context(row, frame))
    first = worker.run_pending(ledger, limit=1, budget_s=180)
    assert first["defer_reasons"]["prepare_context_load_superseded"] == 1
    claim = _superseded_claim(ledger, row)
    assert claim and claim["next_try"] > time.time() + 5 * 3600

    held, _ = worker._select_pending(ledger, 1, None, None, False)
    assert held == []

    corrected = _multi_piece_body() + "\n合成の訂正を追加する。"
    digest = hashlib.sha256(corrected.encode()).hexdigest()
    with ledger.db:
        ledger.db.execute(
            "UPDATE messages SET body_text=?, content_hash=? WHERE message_id=?",
            (corrected, digest, row["message_id"]))
    # the stale claim does not hide the corrected body: it is re-selected and
    # re-evaluated instead of waiting out the revive interval
    rows, _ = worker._select_pending(ledger, 1, None, None, False)
    assert [r["message_id"] for r in rows] == [row["message_id"]]
    # and the stale claim is taken over: the new claim binds the new body
    lease = extract_llm_runner._claim(ledger, dict(rows[0]), 60)
    assert lease is not None
    claim = ledger.db.execute(
        "SELECT json_extract(payload,'$.hash') AS h FROM fetch_jobs "
        "WHERE kind='extract_claim' AND project_id=? AND message_id=?",
        (row["project_id"], row["message_id"])).fetchone()
    assert claim["h"] == digest
    hidden, _ = worker._select_pending(ledger, 1, None, None, False)
    assert hidden == []


def test_superseded_hold_shortens_when_the_pending_set_changes(routed):
    """The long wait is conditional: it lasts only while every collectible
    pending record still binds a retired generation."""
    ledger, *_ = routed
    row, frame = _frozen(ledger, body=_multi_piece_body())
    _persist(ledger, row, frame, _old_context(row, frame))
    held = {**_scan_row(ledger, row), "_superseded_local": True}
    reasons = ("prepare_context_load_superseded",)
    assert extract_llm_runner._prepare_hold_s(ledger, held, reasons) \
        == extract_llm_runner.REVIVE_COOLDOWN_S
    # a pending record a current contract can bind ends the long wait
    _persist(ledger, row, frame, _current_context(row, frame))
    assert extract_llm_runner._prepare_hold_s(ledger, held, reasons) \
        == extract_llm_runner._PREPARE_HOLD_S
    # a row that was never tagged superseded keeps the short hold
    assert extract_llm_runner._prepare_hold_s(ledger, _scan_row(ledger, row), reasons) \
        == extract_llm_runner._PREPARE_HOLD_S


def test_other_prepare_refusals_keep_no_lookahead_claim(routed, monkeypatch):
    """Regression check: in the cloud lookahead, a prepare refusal outside the
    hold set (here a prepare budget refusal) keeps main's behavior — no claim
    is placed, so the next pass re-selects the row immediately instead of
    waiting out a preparation hold."""
    ledger, cfg, calls, reserved, results = routed
    cfg["cloud_llm"]["max_concurrency"] = 32
    row, frame = _frozen(ledger, body=_multi_piece_body(), mid=2)
    _persist(ledger, row, frame, _old_context(row, frame))
    old(ledger, 1)
    now = time.time()
    with ledger.db:
        ledger.db.execute("UPDATE messages SET first_seen=?,posted_at_ts=?", (now - 1200, now - 1200))
        ledger.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=2", (now - 1100,))
        ledger.db.execute("UPDATE message_revisions SET observed_at=?", (now - 1200,))
        cloud_policy.capture_history_tx(ledger.db, "synthetic-refused-head", now)
    monkeypatch.setattr(worker, "_probe_format", lambda **kw: pytest.fail("cloud probed local backend"))
    monkeypatch.setattr(extract_llm_provider, '_probe_format', worker._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', worker._probe_format)

    def _refuse(led, rows, target, prefilter, **kw):
        kw['blocked_rows'].append((dict(rows[0]), "prepare_context_prepare_budget_exceeded"))
        kw['blocked_reasons']["prepare_context_prepare_budget_exceeded"] = 1
        return [], 0, 1

    monkeypatch.setattr(extract_llm_runner, "_prepare_jobs", _refuse)
    with legacy_cloud_routes():
        first = worker.run_pending(ledger, limit=1, workers=1, budget_s=180,
            background=True, cloud_only=True, prioritize_patients=True)
    assert first["defer_reasons"]["prepare_context_prepare_budget_exceeded"] >= 1
    assert _superseded_claim(ledger, row) is None


def test_superseded_head_keeps_one_claim_and_the_cloud_tail_runs(
        routed, monkeypatch):
    """Cloud lane: a still-superseded head takes one bounded claim in the
    lookahead instead of re-blocking every pass; the next pass sees the
    unclaimed tail instead of re-loading the same context."""
    ledger, cfg, calls, reserved, results = routed
    cfg["cloud_llm"]["max_concurrency"] = 32
    row, frame = _frozen(ledger, body=_multi_piece_body(), mid=2)
    _persist(ledger, row, frame, _old_context(row, frame))
    old(ledger, 1)
    now = time.time()
    with ledger.db:
        ledger.db.execute("UPDATE messages SET first_seen=?,posted_at_ts=?", (now - 1200, now - 1200))
        # newest-first selection: the superseded row must be the head
        ledger.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=2", (now - 1100,))
        ledger.db.execute("UPDATE message_revisions SET observed_at=?", (now - 1200,))
        cloud_policy.capture_history_tx(ledger.db, "synthetic-superseded-head", now)
    monkeypatch.setattr(worker, "_probe_format", lambda **kw: pytest.fail("cloud probed local backend"))
    monkeypatch.setattr(extract_llm_provider, '_probe_format', worker._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', worker._probe_format)

    def _pass():
        with legacy_cloud_routes():
            return worker.run_pending(ledger, limit=1, workers=1, budget_s=180,
                background=True, cloud_only=True, prioritize_patients=True)

    first = _pass()
    # the blocked head frees its slot inside the same pass: the tail completes
    assert first["defer_reasons"] == {"prepare_context_load_superseded": 1}
    assert first["done"] == 1
    claim = _superseded_claim(ledger, row)
    assert claim and claim["next_try"] > time.time() + 5 * 3600
    second = _pass()
    # the held head is not re-selected or re-prepared by the next pass
    assert second["selected"] == 0 and second["deferred"] == 0
    assert len(calls) == len(reserved) == len(results) == 1
