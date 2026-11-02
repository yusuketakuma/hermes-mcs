"""Cloud selection progresses past unreceipted replies without widening history scope."""
import extraction_context
from contextlib import closing, contextmanager
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
import semantic_drain
from semantic_testkit import _cfg, _ledger, _message, _patient
from test_anthropic_runtime import _assert_legacy_retained, _current_artifacts, old, routed

__all__ = ["routed"]

_REAL_RESERVER = cloud_policy.usage_reserver
_REAL_RESULT = cloud_policy.record_result
_REAL_BLOCK = cloud_policy.block_reason


@pytest.fixture
def blocked_prefix(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    cfg = _cfg("assist", fact_source="shadow", summary_mode="shadow", loop_mode="shadow")
    cfg["cloud_llm"] = {"mode": "on", "project_ids": None}
    monkeypatch.setattr(extract_llm, "load_config", lambda *a: cfg)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda *a: cfg)
    monkeypatch.setattr(semantic_drain, "load_config", lambda *a: cfg)
    monkeypatch.setattr(cloud_policy, "api_key", lambda *a: "synthetic-key")
    for pid in range(1, 514):
        _patient(led, pid)
        led.save_messages([_message(pid, pid=pid, body="完全合成の過去記録。")])
        led.semantic_seed(pid, [pid], {"source": "history_import"})
    with led.db:
        led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=513")
        led.db.execute("UPDATE messages SET first_seen=?,posted_at_ts=message_id", (time.time() - 1200,))
        led.db.execute("UPDATE message_revisions SET observed_at=?", (time.time() - 1200,))
        cloud_policy.capture_history_tx(led.db, "synthetic-selection-scope", time.time())
    # Roots still occur in the receipt, but their new reply does not. A root-only
    # membership test cannot authorize the expanded thread as a cloud prompt.
    for pid in range(1, 513):
        led.save_messages([_message(1000 + pid, pid=pid, parent=pid,
                                   body="完全合成の承認範囲外の新しい返信。")])
    yield led, cfg
    led.close()


def _select(led, lane, *, deadline=None):
    if lane == "semantic":
        return semantic_drain._select_cloud_due(led, "backlog", ("semantic",), False, 1,
            deadline=deadline, isolated=deadline is not None)[0]
    return extract_llm._select_pending(led, 64, None, None, False,
        cloud_only=True, prioritize_patients=True, deadline=deadline)[0]


@pytest.mark.parametrize("lane", ["semantic", "extract"])
def test_receipted_work_behind_large_invalid_prefix_eventually_selected(blocked_prefix, lane):
    led, _ = blocked_prefix
    observed = []
    # Permit a finite paging implementation rather than demanding an unbounded
    # scan on the first call. Each pass must retain the exact source check.
    for _ in range(12):
        observed.extend(row for row in _select(led, lane)
                        if cloud_policy.history_thread_allowed(led.db, row["project_id"], row["message_id"]))
        if observed:
            break
    assert [row["project_id"] for row in observed] == [513]
    assert all(row["message_id"] == 513 for row in observed)


@pytest.mark.parametrize("lane", ["semantic", "extract"])
def test_isolated_selection_reaches_receipted_tail(blocked_prefix, lane):
    led, _ = blocked_prefix
    selected = _select(led, lane, deadline=time.monotonic() + 60)
    assert [row["project_id"] for row in selected] == [513]
    assert cloud_policy.history_thread_allowed(led.db, 513, 513)


@pytest.mark.parametrize("lane", ["semantic", "extract"])
def test_expired_selection_deadline_is_not_an_empty_success(blocked_prefix, lane):
    led, _ = blocked_prefix
    module = semantic_drain if lane == "semantic" else extract_llm
    with pytest.raises(module._CloudSelectionIncomplete, match="^deadline$"):
        _select(led, lane, deadline=time.monotonic() - 1)


@pytest.mark.parametrize("lane", ["semantic", "extract"])
def test_selection_deadline_during_candidate_scan_is_incomplete(blocked_prefix, monkeypatch, lane):
    led, _ = blocked_prefix
    clock = [time.monotonic()]
    deadline = clock[0] + 10
    module = semantic_drain if lane == "semantic" else extract_llm
    monkeypatch.setattr(module, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=time.time, perf_counter=time.perf_counter))
    monkeypatch.setattr(cloud_inference, 'time', module.time)
    original = cloud_policy.history_target_ready
    checked = []

    def expire_after_first(db, pid, mid):
        checked.append(mid)
        result = original(db, pid, mid)
        clock[0] = deadline + 1
        return result

    monkeypatch.setattr(cloud_policy, "history_target_ready", expire_after_first)
    with pytest.raises(module._CloudSelectionIncomplete, match="^deadline$"):
        _select(led, lane, deadline=deadline)
    assert checked


def test_extract_selection_interrupts_sql_before_candidate_iteration(blocked_prefix, monkeypatch):
    led, _ = blocked_prefix
    clock = [time.monotonic()]
    deadline = clock[0] + 10
    connect, opened = sqlite3.connect, []

    def reader(*args, **kwargs):
        db = connect(*args, **kwargs)
        opened.append(db)
        def expire(sql):
            if sql.lstrip().startswith("SELECT m.message_id, m.project_id,"):
                clock[0] = deadline + 1
        db.set_trace_callback(expire)
        return db

    monkeypatch.setattr(extract_llm.sqlite3, "connect", reader)
    monkeypatch.setattr(extract_llm, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    with pytest.raises(extract_llm._CloudSelectionIncomplete, match="^deadline$") as error:
        _select(led, "extract", deadline=deadline)
    assert isinstance(error.value.__cause__, sqlite3.OperationalError)
    assert "interrupt" in str(error.value.__cause__)
    assert len(opened) == 1 and not led.db.in_transaction
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


@pytest.mark.parametrize("kind", ["memory", "transaction"])
def test_bounded_extract_selection_never_falls_back_to_the_owner_connection(tmp_path, kind):
    led = SimpleNamespace(db=sqlite3.connect(":memory:")) if kind == "memory" else _ledger(tmp_path)
    try:
        if kind == "transaction":
            led.db.execute("BEGIN")
        reason = "reader_unavailable" if kind == "memory" else "active_transaction"
        with pytest.raises(extract_llm._CloudSelectionIncomplete, match="^" + reason + "$"):
            _select(led, "extract", deadline=time.monotonic() + 60)
        assert led.db.in_transaction is (kind == "transaction")
        assert led.db.execute("SELECT 1").fetchone()[0] == 1
    finally:
        led.db.close()


def test_bounded_extract_selection_preserves_owner_cancellation(tmp_path):
    with closing(_ledger(tmp_path)) as led:
        led.db.set_progress_handler(lambda: 1, 1)
        try:
            with pytest.raises(sqlite3.OperationalError, match="interrupt"):
                _select(led, "extract", deadline=time.monotonic() + 60)
            with pytest.raises(sqlite3.OperationalError, match="interrupt"):
                led.db.execute("SELECT 1").fetchone()
        finally:
            led.db.set_progress_handler(None, 0)


@pytest.mark.parametrize("lane", ["semantic", "extract"])
def test_revoked_history_snapshot_never_selects_previously_allowed_tail(blocked_prefix, lane):
    led, _ = blocked_prefix
    for _ in range(2):
        _select(led, lane)
    with led.db:
        led.artifact_add_tx("cloud_history_scope", '{"version":1,"sources":{}}')
    for _ in range(12):
        assert not [row for row in _select(led, lane)
                    if cloud_policy.history_thread_allowed(led.db, row["project_id"], row["message_id"])]


@pytest.mark.parametrize("raw", [
    '{"version":true,"sources":{"1:1":"' + "a" * 64 + '"}}',
    '{"version":1,"version":1,"sources":{"1:1":"' + "a" * 64 + '"}}',
    '{"version":1,"sources":{"1:1":"' + "a" * 64 + '","1:1":"' + "b" * 64 + '"}}',
])
def test_ambiguous_history_receipt_never_authorizes_source(raw):
    assert cloud_policy._history_sources(raw) == {}


@pytest.fixture
def cloud_candidates(routed, monkeypatch):
    led, cfg, *_ = routed
    cfg["cloud_llm"]["max_concurrency"] = 32
    for mid in range(1, 65):
        old(led, mid)
    with led.db:
        led.db.execute("UPDATE messages SET first_seen=?", (time.time() - 1200,))
        led.db.execute("UPDATE message_revisions SET observed_at=?", (time.time() - 1200,))
        cloud_policy.capture_history_tx(led.db, "synthetic-claim-refill", time.time())
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: pytest.fail("cloud probed local backend"))
    monkeypatch.setattr(extract_llm, "_run_batch_call", lambda *a: pytest.fail("cloud used local batch"))
    return routed


def _cloud_pass(led, *, limit=1):
    return extract_llm.run_pending(led, limit=limit, budget_s=180, background=True,
        cloud_only=True, prioritize_patients=True, batch_k=4)


def _claim_count(led):
    return led.db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0]


def test_bounded_extract_selection_preserves_owner_cursor_and_handler(cloud_candidates, monkeypatch):
    led, *_ = cloud_candidates
    connect, opened, progress = sqlite3.connect, [], []

    def reader(database, **kwargs):
        assert database.endswith("?mode=ro") and kwargs == {"uri": True, "timeout": 0}
        db = connect(database, **kwargs)
        opened.append(db)
        return db

    monkeypatch.setattr(extract_llm.sqlite3, "connect", reader)
    led.db.set_progress_handler(lambda: progress.append(1) and 0, 1)
    cursor = led.db.execute("SELECT 10 UNION ALL SELECT 20 UNION ALL SELECT 30")
    try:
        assert cursor.fetchone()[0] == 10
        assert len(_select(led, "extract", deadline=time.monotonic() + 60)) == 64
        assert [row[0] for row in cursor] == [20, 30]
        before = len(progress)
        led.db.execute("SELECT 1").fetchone()
        assert len(progress) > before and not led.db.in_transaction
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            opened[0].execute("SELECT 1")
    finally:
        cursor.close()
        led.db.set_progress_handler(None, 0)


@pytest.mark.parametrize("primary", [None, ValueError, KeyboardInterrupt])
def test_selection_reader_close_failure_preserves_the_primary_error(cloud_candidates, monkeypatch, primary):
    led, *_ = cloud_candidates
    connect, closed = sqlite3.connect, []

    class Reader(sqlite3.Connection):
        def close(self):
            super().close()
            closed.append(True)
            raise sqlite3.OperationalError("synthetic-close")

    monkeypatch.setattr(extract_llm.sqlite3, "connect",
                        lambda *a, **kw: connect(*a, **kw, factory=Reader))
    if primary is not None:
        def failed(*args, **kwargs):
            raise primary("synthetic-primary")
        monkeypatch.setattr(extract_llm, "_select_pending_impl", failed)
    expected = primary or extract_llm._CloudSelectionIncomplete
    reason = "synthetic-primary" if primary is not None else "reader_close_failed"
    with pytest.raises(expected, match="^" + reason + "$"):
        _select(led, "extract", deadline=time.monotonic() + 60)
    assert closed == [True] and led.db.execute("SELECT 1").fetchone()[0] == 1


def test_selection_rejects_a_replaced_reader_file(cloud_candidates, monkeypatch):
    led, *_ = cloud_candidates
    identities = iter([SimpleNamespace(st_dev=1, st_ino=1), SimpleNamespace(st_dev=1, st_ino=2)])
    monkeypatch.setattr(extract_llm, "os", SimpleNamespace(stat=lambda _: next(identities)))
    with pytest.raises(extract_llm._CloudSelectionIncomplete, match="^reader_changed$"):
        _select(led, "extract", deadline=time.monotonic() + 60)
    assert led.db.execute("SELECT 1").fetchone()[0] == 1


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


def test_retained_extract_pass_skips_selection_and_preserves_unknown_counts(cloud_candidates, monkeypatch):
    led, carry, _, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    clock = [time.monotonic()]
    observed = []

    def incomplete(*args, **kwargs):
        observed.append(kwargs.get("deadline"))
        raise extract_llm._CloudSelectionIncomplete("deadline")

    monkeypatch.setattr(extract_llm, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm, "_select_pending", incomplete)
    monkeypatch.setattr(extract_llm, "_prepare_jobs", lambda *a, **kw: pytest.fail("incomplete selection prepared work"))
    report = extract_llm.run_pending(led, limit=1, budget_s=180, cloud_only=True,
                                     per_write_lock=True, carry=carry)
    assert observed == [] and report["carry"] is carry
    assert report["carry_waiting"] is True and "cloud_selection_incomplete" not in report
    assert report["selected"] == report["fresh_selected"] == 0
    assert report["left"] is report["left_estimate"] is None
    assert report["left_estimate_age_s"] is None and report["queue_ages_s"] == {"oldest": None, "newest": None}
    assert report["done"] == 0 and len(calls) == len(reservations) == len(results) == 1
    assert len(carry) == 1 and _claim_count(led) == 1


def test_empty_carry_still_reports_incomplete_selection(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    carry = []
    observed = []

    def incomplete(*args, **kwargs):
        observed.append(kwargs["deadline"])
        raise extract_llm._CloudSelectionIncomplete("deadline")

    monkeypatch.setattr(extract_llm, "_select_pending", incomplete)
    monkeypatch.setattr(extract_llm, "_prepare_jobs",
                        lambda *a, **kw: pytest.fail("incomplete selection prepared work"))
    report = extract_llm.run_pending(led, limit=1, budget_s=180, cloud_only=True,
                                    per_write_lock=True, carry=carry)
    assert len(observed) == 1 and report["carry"] is carry
    assert report["cloud_selection_incomplete"] is True and report["cloud_selection_reason"] == "deadline"
    assert report["selected"] is report["fresh_selected"] is report["left"] is report["left_estimate"] is None
    assert report["left_estimate_age_s"] is None and report["queue_ages_s"] == {"oldest": None, "newest": None}
    assert report["done"] == 0 and calls == reservations == results == []
    assert _claim_count(led) == 0


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
    report = _cloud_pass(led)
    assert report["done"] == 1 and report["selected"] == 64
    assert report["context_blocked"] == report["deferred"] == blocked_count
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
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm, "_select_pending", selected)
    monkeypatch.setattr(extract_llm, "_prepare_jobs", preparing)
    monkeypatch.setattr(extract_llm, "_claim", lambda *a: pytest.fail("expired pass claimed a source"))
    assert _cloud_pass(led)["done"] == 0
    assert len(prepared) == int(expires_after == "preparation")
    assert calls == reservations == results == [] and _claim_count(led) == 0


def test_empty_cloud_pass_does_not_count_the_global_backlog(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    monkeypatch.setattr(extract_llm, "_backlog_report",
                        lambda *a: pytest.fail("cloud pass counted the global backlog"))
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


@pytest.mark.parametrize("change", ["completed", "retry"])
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
            ledger.artifact_add(extract_llm.KIND, '{"symptoms":[]}',
                                project_id=row["project_id"], message_id=first, meta=meta)
            changed.append(first)
        return original_claim(ledger, row, lease_s)

    monkeypatch.setattr(extract_llm, "_claim", race)
    report = _cloud_pass(led)
    assert changed == [first] and report["done"] == 1
    assert len(calls) == len(reservations) == len(results) == 1
    assert reservations[0]["identity"]["message_id"] == selected[1]["message_id"]
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
    monkeypatch.setattr(extract_llm, "_capture_checkpoint_state", after_claim)
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
    monkeypatch.setattr(extract_llm, "_prepare_jobs", preparing)
    monkeypatch.setattr(extract_llm, "_capture_checkpoint_state", after_claim)
    monkeypatch.setattr(extract_llm, "_ensure_v1", rules)
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
    assert _cloud_pass(led)["done"] == 0 and len(changed) == 1
    assert sent == [] and _claim_count(led) == 0 and not _current_artifacts(led)
    _assert_legacy_retained(led, prior)
    usage = cloud_policy.usage_report(led)
    assert not usage["pricing_unverified"]
    assert usage["unresolved_requests"] == len(unresolved) == int(change == "unknown_paid")
    if unresolved:
        assert usage["daily_requests"] == 1 and Decimal(usage["max_reserved_cost_usd"]) > 0
        assert not led.artifacts("cloud_usage_result")
    else:
        assert usage["daily_requests"] == 0


@pytest.mark.parametrize('oldest_first', [False, True])
def test_cloud_priority_walk_matches_fixed_snapshot_order(cloud_candidates, oldest_first):
    led, cfg, *_ = cloud_candidates
    cfg['cloud_llm']['project_ids'] = [1, 2, 3]
    for pid in (2, 3):
        _patient(led, pid)
        for mid in (100 + pid * 10, 101 + pid * 10):
            led.save_messages([_message(mid, pid=pid, body='完全合成の過去記録。')])
    with led.db:
        led.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        led.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        led.db.execute('UPDATE messages SET posted_at_ts=NULL WHERE message_id IN (1,120,130,131)')
        led.db.execute('UPDATE messages SET posted_at_ts=17 WHERE message_id IN (2,3,121)')
        led.db.execute('UPDATE patients SET is_archived=1 WHERE project_id=3')
        cloud_policy.capture_history_tx(led.db, 'synthetic-order', time.time())
    args = (led, 100, None, None, oldest_first)
    kwargs = {'cloud_only': True, 'prioritize_patients': True}
    expected = extract_llm._select_pending(*args, **kwargs)[0]
    actual = extract_llm._select_pending(*args, **kwargs, deadline=time.monotonic() + 120)[0]
    assert [r['message_id'] for r in actual] == [r['message_id'] for r in expected]
    assert len(actual) == 68


def test_cloud_priority_walk_resumes_across_deadlines_and_unrelated_writes(cloud_candidates, monkeypatch):
    led, _, *_ = cloud_candidates
    expected = [row['message_id'] for row in _select(led, 'extract')]
    clock = [time.monotonic()]
    real_connect = sqlite3.connect
    scans = []

    def connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        def trace(sql):
            if sql.startswith('SELECT message_id,project_id,posted_at_ts FROM messages'):
                scans.append(sql)
                clock[0] += 0.4
        db.set_trace_callback(trace)
        return db

    monkeypatch.setattr(extract_llm.sqlite3, 'connect', connect)
    monkeypatch.setattr(extract_llm, '_CLOUD_PRIORITY_PAGE', 8)
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    positions, observed = [], []
    for _ in range(200):
        try:
            actual = _select(led, 'extract', deadline=clock[0] + 1)
        except extract_llm._CloudSelectionIncomplete as error:
            assert str(error) == 'deadline'
            positions.append(led._cloud_priority_progress['after'])
            led.artifact_add('synthetic_unrelated_observation', '{}')
            clock[0] += 1
        else:
            for row in actual:
                observed.append(row['message_id'])
                led.artifact_add(extract_llm.KIND, '{"symptoms":[]}', project_id=row['project_id'],
                    message_id=row['message_id'], meta={'hash': row['content_hash'],
                    'extract_version': extract_llm.EXTRACT_VERSION,
                    extract_llm.inference.VERSION_KEY: extract_llm._INFERENCE_VERSION})
            if observed == expected:
                break
            clock[0] += 1
    else:
        pytest.fail('bounded walk did not reach its finite highwater')
    assert positions == sorted(set(positions)) and positions[-1] >= 56
    assert len(scans) > 2 and observed == expected and len(observed) == len(set(observed))


@pytest.mark.parametrize('change', ['scope', 'config', 'db'])
def test_cloud_priority_scope_change_invalidates_the_saved_walk(cloud_candidates, monkeypatch, change):
    led, cfg, *_ = cloud_candidates
    clock = [time.monotonic()]
    real_connect = sqlite3.connect
    def connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        def trace(sql):
            if sql.startswith('SELECT message_id,project_id,posted_at_ts FROM messages'):
                clock[0] += 0.4
        db.set_trace_callback(trace)
        return db
    monkeypatch.setattr(extract_llm.sqlite3, 'connect', connect)
    monkeypatch.setattr(extract_llm, '_CLOUD_PRIORITY_PAGE', 8)
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    inode = [1]
    if change == 'db':
        monkeypatch.setattr(extract_llm, 'os', SimpleNamespace(stat=lambda _: SimpleNamespace(st_dev=1, st_ino=inode[0])))
    with pytest.raises(extract_llm._CloudSelectionIncomplete, match='deadline'):
        _select(led, 'extract', deadline=clock[0] + 1)
    previous = led._cloud_priority_progress
    if change == 'scope':
        led.artifact_add('cloud_history_scope', '{"version":1,"sources":{}}')
    elif change == 'config':
        cfg['cloud_llm']['exclude_project_ids'] = [1]
    else:
        inode[0] = 2
    clock[0] += 1
    try:
        rows = _select(led, 'extract', deadline=clock[0] + 120)
    except extract_llm._CloudSelectionIncomplete:
        rows = []
    assert len(rows) == (64 if change == 'db' else 0)
    assert led._cloud_priority_progress is not previous


def test_cloud_priority_retained_candidate_is_revalidated_after_deadline(cloud_candidates, monkeypatch):
    led, _, *_ = cloud_candidates
    clock = [time.monotonic()]
    ready = cloud_policy.history_target_ready
    calls = []
    def expires_after_second(db, pid, mid):
        result = ready(db, pid, mid)
        calls.append(mid)
        if len(calls) == 2:
            clock[0] += 10
        return result
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    monkeypatch.setattr(cloud_policy, 'history_target_ready', expires_after_second)
    with pytest.raises(extract_llm._CloudSelectionIncomplete, match='deadline'):
        _select(led, 'extract', deadline=clock[0] + 5)
    first = led._cloud_priority_progress['ready'][0][0]
    with led.db:
        led.db.execute('UPDATE messages SET body_state=? WHERE message_id=?', ('deleted', first))
    rows = _select(led, 'extract', deadline=clock[0] + 120)
    assert first not in [row['message_id'] for row in rows]


def test_cloud_priority_interrupted_metadata_query_retries_same_page(cloud_candidates, monkeypatch):
    led, _, *_ = cloud_candidates
    clock = [time.monotonic()]
    real_connect, interrupted = sqlite3.connect, []
    def connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        def trace(sql):
            if not interrupted and sql.startswith('SELECT message_id,project_id,posted_at_ts FROM messages'):
                interrupted.append(True)
                clock[0] += 20
        db.set_trace_callback(trace)
        return db
    monkeypatch.setattr(extract_llm.sqlite3, 'connect', connect)
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    with pytest.raises(extract_llm._CloudSelectionIncomplete, match='deadline'):
        _select(led, 'extract', deadline=clock[0] + 10)
    assert interrupted and led._cloud_priority_progress['after'] is None
    rows = _select(led, 'extract', deadline=clock[0] + 120)
    assert len(rows) == 64 and len({r['message_id'] for r in rows}) == 64


def test_cloud_priority_wrap_discovers_new_rows_after_fixed_highwater(cloud_candidates):
    led, _, *_ = cloud_candidates
    first = _select(led, 'extract', deadline=time.monotonic() + 120)
    assert len(first) == 64
    old(led, 65)
    with led.db:
        led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=65', (time.time() - 1200,))
        led.db.execute('UPDATE message_revisions SET observed_at=? WHERE message_id=65', (time.time() - 1200,))
        for row in first:
            led.artifact_add_tx(extract_llm.KIND, '{"symptoms":[]}', project_id=row['project_id'],
                message_id=row['message_id'], meta={'hash': row['content_hash'],
                'extract_version': extract_llm.EXTRACT_VERSION,
                extract_llm.inference.VERSION_KEY: extract_llm._INFERENCE_VERSION})
    assert _select(led, 'extract', deadline=time.monotonic() + 120) == []
    after_wrap = _select(led, 'extract', deadline=time.monotonic() + 120)
    assert all(row['message_id'] != 65 for row in after_wrap)  # not in the old receipt
    with led.db:
        led.db.execute('BEGIN IMMEDIATE')
        cloud_policy.capture_history_tx(led.db, 'synthetic-new-scope', time.time())
    # A new receipt invalidates the old hint, so the new occurrence is checked afresh.
    rows = _select(led, 'extract', deadline=time.monotonic() + 120)
    assert any(row['message_id'] == 65 for row in rows)


def test_cloud_priority_consumes_all_selected_in_fixed_order_over_single_unit_passes(routed):
    led, _, calls, reservations, results = routed
    for mid in range(1, 6):
        old(led, mid)
    with led.db:
        led.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        led.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        cloud_policy.capture_history_tx(led.db, 'synthetic-sequential', time.time())
    expected = [row['message_id'] for row in _select(led, 'extract')]
    for _ in expected:
        assert _cloud_pass(led)['done'] == 1
    actual = [r['identity']['message_id'] for r in reservations]
    assert actual == expected and len(actual) == len(set(actual)) == 5
    assert len(calls) == len(results) == 5 and _claim_count(led) == 0


def test_cloud_priority_short_budget_keeps_selected_candidates_for_next_pass(cloud_candidates):
    led, _, calls, reservations, results = cloud_candidates
    expected = [row['message_id'] for row in _select(led, 'extract')]
    report = extract_llm.run_pending(led, limit=1, budget_s=50, background=True,
        cloud_only=True, prioritize_patients=True)
    assert report['done'] == 0 and calls == reservations == results == []
    assert _claim_count(led) == 0
    assert _cloud_pass(led)['done'] == 1
    assert reservations[0]['identity']['message_id'] == expected[0]


def _make_paid_carry(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    lock = {'held': False}
    @contextmanager
    def write_lock(enabled, wait_s=60, **kwargs):
        yield lock['held'] or not enabled
    monkeypatch.setattr(extract_llm, '_write_lock', write_lock)
    first = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=[])
    assert first['done'] == 0 and len(first['carry']) == 1
    assert len(calls) == len(reservations) == len(results) == 1
    return led, first['carry'], lock, calls, reservations, results


def test_incomplete_selection_renews_then_saves_carried_paid_result(cloud_candidates, monkeypatch):
    led, carry, lock, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    wall = [time.time()]
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(
        monotonic=time.monotonic, time=lambda: wall[0], perf_counter=time.perf_counter))
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    def incomplete(*args, **kwargs):
        raise extract_llm._CloudSelectionIncomplete('deadline')
    monkeypatch.setattr(extract_llm, '_select_pending', incomplete)
    for _ in range(2):
        wall[0] += 300
        result = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
            cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=carry)
        assert 'cloud_selection_incomplete' not in result and result['done'] == 0
        assert result['selected'] == result['fresh_selected'] == 0
        assert result['left'] is result['left_estimate'] is None
        assert result['carry'] is carry and len(carry) == 1 and carry[0][1] > wall[0] + 120
        assert len(calls) == len(reservations) == len(results) == 1
    lock['held'] = True
    result = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=carry)
    assert 'cloud_selection_incomplete' not in result and result['done'] == result['carried'] == 1
    assert result['selected'] == result['fresh_selected'] == 0
    assert carry == [] and len(_current_artifacts(led)) == 1 and _claim_count(led) == 0
    assert len(calls) == len(reservations) == len(results) == 1


def test_short_incomplete_ticks_preserve_carry_but_expired_lease_is_never_committed(cloud_candidates, monkeypatch):
    led, carry, lock, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    # Persist a real synthetic accounting pair for this request; the LLM
    # transport remains stubbed and no external endpoint is contacted.
    cfg = cloud_candidates[1]
    mid = carry[0][0]['message_id']
    body = AnthropicClient('synthetic').body(calls[0], extract_llm.MAX_TOKENS, None)
    reserve = _REAL_RESERVER(led, cfg, route='reextract', project_id=1, message_id=mid, unit_key=str(mid))
    receipt = reserve(body, 2)
    _REAL_RESULT(led, receipt, {'model': receipt['model'], 'request_fp': receipt['request_fp'],
        'usage': {'input_tokens': 10, 'output_tokens': 5}, 'http_class': 'http_200',
        'request_id': 'synthetic', 'error_kind': None})
    wall = [time.time()]
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(
        monotonic=time.monotonic, time=lambda: wall[0], perf_counter=time.perf_counter))
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    def incomplete(*args, **kwargs):
        raise extract_llm._CloudSelectionIncomplete('deadline')
    monkeypatch.setattr(extract_llm, '_select_pending', incomplete)
    lease = carry[0][1]
    for now in (lease - 10, lease + 1):
        wall[0] = now
        result = extract_llm.run_pending(led, limit=1, budget_s=0.5, background=True,
            cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=carry)
        assert 'cloud_selection_incomplete' not in result and result['carry'] is carry and len(carry) == 1
        assert result['selected'] == result['fresh_selected'] == 0
        assert result['left'] is result['left_estimate'] is None
        assert result['done'] == 0 and len(calls) == len(reservations) == len(results) == 1
    lock['held'] = True
    result = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=carry)
    assert 'cloud_selection_incomplete' not in result and result['done'] == 0
    assert result['selected'] == result['fresh_selected'] == 0
    assert result['deferred'] == 1 and carry == [] and not _current_artifacts(led)
    assert len(calls) == len(reservations) == len(results) == 1
    from anthropic_client import AnthropicError
    with pytest.raises(AnthropicError, match='budget_exceeded'):
        reserve(body, 2)
    assert len(led.artifacts('cloud_usage_reserve')) == len(led.artifacts('cloud_usage_result')) == 1


@pytest.mark.parametrize('budget', [0.5, 180])
def test_successful_selection_retains_unsettled_carry_until_the_next_pass(cloud_candidates, monkeypatch, budget):
    led, carry, lock, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    monkeypatch.setattr(extract_llm, '_select_pending', lambda *args, **kwargs: ([], 0))
    before = carry[0][1]
    result = extract_llm.run_pending(led, limit=1, budget_s=budget, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=carry)
    retained = result['carry']
    assert result['done'] == 0 and len(retained) == 1 and retained[0][1] >= before
    row = led.db.execute("SELECT next_try FROM fetch_jobs WHERE kind='extract_claim'").fetchone()
    assert row is not None and row[0] == retained[0][1]
    assert len(calls) == len(reservations) == len(results) == 1
    lock['held'] = True
    result = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=retained)
    assert result['done'] == result['carried'] == 1 and result['carry'] == []
    assert len(_current_artifacts(led)) == 1 and _claim_count(led) == 0
    assert len(calls) == len(reservations) == len(results) == 1


def test_paid_carry_scope_revocation_is_discarded_and_counted(cloud_candidates, monkeypatch):
    led, carry, lock, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    prior = [dict(row) for row in led.artifacts(extract_llm.KIND)]
    cloud_candidates[1]['cloud_llm']['exclude_project_ids'] = [1]
    monkeypatch.setattr(extract_llm, '_select_pending',
        lambda *a, **kw: (_ for _ in ()).throw(extract_llm._CloudSelectionIncomplete('deadline')))
    lock['held'] = True
    result = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=carry)
    assert 'cloud_selection_incomplete' not in result
    assert result['selected'] == result['fresh_selected'] == 0
    assert result['done'] == 0 and result['carried'] == 1 and result['deferred'] == 1
    assert carry == [] and _claim_count(led) == 0
    _assert_legacy_retained(led, prior)
    assert len(led.artifacts('cloud_usage_disposition')) == 1
    assert len(calls) == len(reservations) == len(results) == 1


@pytest.mark.parametrize('wait_s', [0, 0.04, 0.5])
def test_deadline_lock_wait_is_bounded_with_real_contended_lock(tmp_path, monkeypatch, wait_s):
    import os
    import writer_lock
    path = str(tmp_path / 'synthetic-run.lock')
    owner = writer_lock.acquire_run_lock(path)
    assert owner is not None
    clock, sleeps, attempts = [100.0], [], []
    def acquire():
        attempts.append(clock[0])
        return writer_lock.acquire_run_lock(path)
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(extract_llm, 'acquire_run_lock', acquire)
    monkeypatch.setattr(extract_llm, '_writer_current', lambda **kw: True)
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    try:
        with extract_llm._write_lock(True, wait_s=wait_s, deadline=100.1) as held:
            assert not held
        assert attempts and clock[0] <= 100.1
        assert sum(sleeps) == pytest.approx(min(wait_s, 0.1))
        sleeps.clear()
        with extract_llm._write_lock(True, wait_s=wait_s) as held:
            assert not held
        assert sleeps == [1]  # unchanged historical polling, even for zero and fractions
    finally:
        os.close(owner)


def test_carry_deadline_keeps_unprocessed_paid_result_and_lease(cloud_candidates, monkeypatch):
    led, carry, lock, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    second = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=[])
    assert len(second['carry']) == 1 and len(calls) == 2
    carry.extend(second['carry'])
    row, lease, commit = carry[0]
    expected_second = carry[1]
    clock = [100.0]
    def first_commit(**kwargs):
        result = commit(**kwargs)
        clock[0] = 102.0
        return result
    carry[0] = (row, lease, first_commit)
    lock['held'] = True
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], time=time.time, perf_counter=time.perf_counter))
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    settled, count, _ = extract_llm._settle_carry(led, carry, final=False,
        fence=extract_llm._WRITER_STATE, preserve_on_fence_loss=True, deadline=101.0,
        lock_context=extract_llm._write_lock(True), report=False)
    assert settled['done'] == count == 1 and carry == [expected_second]
    assert len(_current_artifacts(led)) == 1 and _claim_count(led) == 1
    assert len(calls) == len(reservations) == len(results) == 2


def test_carry_partial_success_then_sqlite_error_retries_only_remaining(cloud_candidates, monkeypatch):
    led, carry, lock, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    second = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=[])
    assert len(second['carry']) == 1
    row, lease, commit = second['carry'][0]
    attempts = []
    def transient(**kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise sqlite3.OperationalError('synthetic locked')
        return commit(**kwargs)
    carry.extend([(row, lease, transient)])
    lock['held'] = True
    def settle():
        return extract_llm._settle_carry(led, carry, final=False,
            fence=extract_llm._WRITER_STATE, preserve_on_fence_loss=True,
            lock_context=extract_llm._write_lock(True), report=False)
    with pytest.raises(sqlite3.OperationalError, match='synthetic locked'):
        settle()
    assert len(_current_artifacts(led)) == 1 and _claim_count(led) == 1
    assert len(carry) == 1 and carry[0][0] is row
    assert led.artifacts('cloud_usage_disposition') == []
    settled, count, _ = settle()
    assert settled['done'] == count == 1 and carry == []
    assert len(_current_artifacts(led)) == 2 and _claim_count(led) == 0
    assert led.artifacts('cloud_usage_disposition') == []
    assert len(calls) == len(reservations) == len(results) == 2


def test_retained_short_budget_skips_lock_wait_and_preserves_paid_result(
        cloud_candidates, tmp_path, monkeypatch):
    import os
    import writer_lock
    real_write_lock = extract_llm._write_lock
    led, carry, _, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    before = carry[0]
    path = str(tmp_path / 'synthetic-carry-run.lock')
    owner = writer_lock.acquire_run_lock(path)
    assert owner is not None
    clock = [100.0]
    def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(extract_llm, '_write_lock', real_write_lock)
    monkeypatch.setattr(extract_llm, 'acquire_run_lock', lambda: writer_lock.acquire_run_lock(path))
    monkeypatch.setattr(extract_llm, '_select_pending', lambda *a, **kw:
                        pytest.fail('retained pass must not select new work'))
    monkeypatch.setattr(extract_llm, '_gc_stale_artifacts', lambda *a, **kw: None)
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=sleep, time=time.time, perf_counter=time.perf_counter))
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    try:
        report = extract_llm.run_pending(led, limit=1, budget_s=0.5, background=True,
            cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=carry)
        assert clock[0] == 100.0
        assert report['selected'] == report['fresh_selected'] == 0
        assert report['left'] is report['left_estimate'] is None
        assert report['done'] == 0 and report['carry'] == [before]
        assert _claim_count(led) == 1 and not _current_artifacts(led)
        assert len(calls) == len(reservations) == len(results) == 1
    finally:
        os.close(owner)


def test_paid_checkpoint_replays_after_later_candidate_database_error(cloud_candidates, monkeypatch):
    led, cfg, _, _, _ = cloud_candidates
    cfg['cloud_llm']['max_concurrency'] = 1
    sent = []
    monkeypatch.setattr(cloud_policy, 'usage_reserver', _REAL_RESERVER)
    monkeypatch.setattr(cloud_policy, 'record_result', _REAL_RESULT)
    monkeypatch.setattr(cloud_policy, 'block_reason', _REAL_BLOCK)
    monkeypatch.setattr(extract_llm.anthropic_client, 'AnthropicClient', AnthropicClient)
    def response(self, method, body, timeout, **kwargs):
        sent.append(body)
        return 200, {}, json.dumps({'id': 'synthetic', 'model': extract_llm.anthropic_client.MODEL,
            'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '{"symptoms":[]}'}],
            'usage': {'input_tokens': 10, 'output_tokens': 5}}).encode()
    monkeypatch.setattr(AnthropicClient, 'request', response)
    lock = {'held': False}
    @contextmanager
    def write_lock(enabled, wait_s=60, **kwargs):
        yield lock['held'] or not enabled
    monkeypatch.setattr(extract_llm, '_write_lock', write_lock)
    actual_extract = extract_llm.llm_extract
    invocations = []
    def fail_second(body, **kwargs):
        invocations.append(True)
        if len(invocations) == 2:
            led.db.execute('SELECT * FROM synthetic_missing_table')
        return actual_extract(body, **kwargs)
    monkeypatch.setattr(extract_llm, 'llm_extract', fail_second)
    with pytest.raises(sqlite3.OperationalError, match='synthetic_missing_table'):
        extract_llm.run_pending(led, limit=2, workers=1, budget_s=180, background=True,
            cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=[])
    assert len(sent) == 1 and _claim_count(led) == 0 and not _current_artifacts(led)
    assert len(led.artifacts('cloud_usage_reserve')) == len(led.artifacts('cloud_usage_result')) == 1
    assert led.artifacts('extract_llm_chunk')
    invocations.clear()
    lock['held'] = True
    with pytest.raises(sqlite3.OperationalError, match='synthetic_missing_table'):
        extract_llm.run_pending(led, limit=2, workers=1, budget_s=180, background=True,
            cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=[])
    assert len(_current_artifacts(led)) == 1 and _claim_count(led) == 0
    assert len(sent) == 1
    assert len(led.artifacts('cloud_usage_reserve')) == len(led.artifacts('cloud_usage_result')) == 1


@pytest.mark.parametrize('updater_changed', [False, True])
def test_cloud_tail_lock_contention_respects_observed_update_fence(
        cloud_candidates, tmp_path, monkeypatch, updater_changed):
    import os
    import writer_lock
    led, _, calls, reservations, results = cloud_candidates
    path = str(tmp_path / 'synthetic-update-run.lock')
    owner = writer_lock.acquire_run_lock(path)
    assert owner is not None
    state, attempts, clock = [(False, 1)], [], [100.0]
    def acquire():
        attempts.append(True)
        if updater_changed and len(attempts) == 2:
            state[0] = (False, 2)
        return writer_lock.acquire_run_lock(path)
    def sleep(seconds):
        clock[0] += seconds
    original_renew, original_release = extract_llm._renew, extract_llm._release
    renewals, releases = [], []
    def renew(*args):
        renewals.append(True)
        return original_renew(*args)
    def release(*args):
        releases.append(True)
        return original_release(*args)
    monkeypatch.setattr(writer_lock, 'updater_state', lambda: state[0])
    monkeypatch.setattr(extract_llm, 'acquire_run_lock', acquire)
    monkeypatch.setattr(extract_llm, '_renew', renew)
    monkeypatch.setattr(extract_llm, '_release', release)
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=sleep, time=time.time, perf_counter=time.perf_counter))
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    try:
        report = extract_llm.run_pending(led, limit=1, budget_s=180, workers=1,
            background=True, cloud_only=True, prioritize_patients=True,
            per_write_lock=True, write_wait_s=1, carry=[])
        assert len(attempts) >= 2 and len(calls) == len(reservations) == len(results) == 1
        assert report['done'] == 0 and len(report['carry']) == 1
        row, lease, _ = report['carry'][0]
        current = led.db.execute("SELECT next_try FROM fetch_jobs WHERE kind='extract_claim' "
                                 "AND project_id=? AND message_id=?",
                                 (row['project_id'], row['message_id'])).fetchone()
        assert current is not None and current[0] == lease
        assert not _current_artifacts(led) and led.artifacts('cloud_usage_disposition') == []
        if updater_changed:
            assert report['updated'] is True and renewals == [] and releases == []
        else:
            assert not report.get('updated') and renewals == [True]
    finally:
        os.close(owner)


@pytest.mark.parametrize('updater_changed', [False, True])
def test_cloud_extract_exception_preserves_claim_only_after_observed_update(
        cloud_candidates, monkeypatch, updater_changed):
    led, cfg, calls, reservations, results = cloud_candidates
    cfg['cloud_llm']['max_concurrency'] = 1
    state, releases = [(False, 1)], []
    original_release = extract_llm._release
    def release(*args):
        releases.append(True)
        return original_release(*args)
    def fail(*args, **kwargs):
        if updater_changed:
            state[0] = (False, 2)
        raise sqlite3.OperationalError('synthetic extract exception')
    monkeypatch.setattr(extract_llm.writer_lock, 'updater_state', lambda: state[0])
    monkeypatch.setattr(extract_llm, '_release', release)
    monkeypatch.setattr(extract_llm, 'llm_extract', fail)
    with pytest.raises(sqlite3.OperationalError, match='synthetic extract exception'):
        extract_llm.run_pending(led, limit=1, workers=1, budget_s=180,
            background=True, cloud_only=True, prioritize_patients=True, carry=[])
    assert _claim_count(led) == int(updater_changed)
    assert len(releases) == int(not updater_changed)
    assert not _current_artifacts(led) and calls == reservations == results == []


@pytest.mark.parametrize('updater_changed', [False, True])
def test_carried_settle_exception_keeps_original_claim_without_new_selection(
        cloud_candidates, monkeypatch, updater_changed):
    led, carry, _, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    cloud_candidates[1]['cloud_llm']['max_concurrency'] = 1
    original_release = extract_llm._release
    state, passes, releases = [(False, 1)], [], []
    def settle(*args, **kwargs):
        passes.append(True)
        if updater_changed:
            state[0] = (False, 2)
        raise sqlite3.OperationalError('synthetic settle exception')
    def release(*args):
        releases.append(True)
        return original_release(*args)
    monkeypatch.setattr(extract_llm.writer_lock, 'updater_state', lambda: state[0])
    monkeypatch.setattr(extract_llm, '_settle_carry', settle)
    monkeypatch.setattr(extract_llm, '_release', release)
    with pytest.raises(sqlite3.OperationalError, match='synthetic settle exception'):
        extract_llm.run_pending(led, limit=1, workers=1, budget_s=180,
            background=True, cloud_only=True, prioritize_patients=True,
            per_write_lock=True, carry=carry)
    assert len(passes) == 1 and len(carry) == 1
    assert _claim_count(led) == 1 and releases == []
    assert len(calls) == len(reservations) == len(results) == 1
    assert not _current_artifacts(led)


@pytest.mark.parametrize('updater_changed', [False, True])
def test_cloud_flush_exception_preserves_claim_only_after_observed_update(
        cloud_candidates, monkeypatch, updater_changed):
    led, cfg, calls, reservations, results = cloud_candidates
    cfg['cloud_llm']['max_concurrency'] = 1
    state, releases = [(False, 1)], []
    original_release = extract_llm._release
    def release(*args):
        releases.append(True)
        return original_release(*args)
    def fail_extract(*args, **kwargs):
        raise RuntimeError('synthetic before flush')
    class FailingQueue:
        def get_nowait(self):
            if updater_changed:
                state[0] = (False, 2)
            raise sqlite3.OperationalError('synthetic flush exception')
    monkeypatch.setattr(extract_llm.writer_lock, 'updater_state', lambda: state[0])
    monkeypatch.setattr(extract_llm, '_release', release)
    monkeypatch.setattr(extract_llm, 'llm_extract', fail_extract)
    monkeypatch.setattr(extract_llm.queue, 'SimpleQueue', FailingQueue)
    with pytest.raises(sqlite3.OperationalError, match='synthetic flush exception'):
        extract_llm.run_pending(led, limit=1, workers=1, budget_s=180,
            background=True, cloud_only=True, prioritize_patients=True, carry=[])
    assert _claim_count(led) == int(updater_changed)
    assert len(releases) == int(not updater_changed)
    assert not _current_artifacts(led) and calls == reservations == results == []


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
    monkeypatch.setattr(extract_llm, '_ensure_v1', fail)
    with pytest.raises(sqlite3.OperationalError, match='synthetic preparation exception'):
        extract_llm.run_pending(led, limit=1, workers=1, budget_s=180,
            background=True, cloud_only=True, prioritize_patients=True, carry=[])
    assert _claim_count(led) == int(updater_changed)
    assert len(releases) == int(not updater_changed)
    assert not _current_artifacts(led) and calls == reservations == results == []


@pytest.mark.parametrize('updater_changed', [False, True])
def test_cloud_handle_exception_preserves_claim_only_after_observed_update(
        cloud_candidates, monkeypatch, updater_changed):
    led, cfg, calls, reservations, results = cloud_candidates
    cfg['cloud_llm']['max_concurrency'] = 1
    state, releases, after_extract = [(False, 1)], [], []
    original_release = extract_llm._release
    original_owns = extract_llm._owns_current_source
    def release(*args):
        releases.append(True)
        return original_release(*args)
    def extract(*args, **kwargs):
        after_extract.append(True)
        return {}
    def owns(*args):
        if after_extract:
            after_extract.append(True)
            if len(after_extract) == 3:
                if updater_changed:
                    state[0] = (False, 2)
                raise sqlite3.OperationalError('synthetic handle exception')
        return original_owns(*args)
    monkeypatch.setattr(extract_llm.writer_lock, 'updater_state', lambda: state[0])
    monkeypatch.setattr(extract_llm, '_release', release)
    monkeypatch.setattr(extract_llm, 'llm_extract', extract)
    monkeypatch.setattr(extract_llm, '_owns_current_source', owns)
    with pytest.raises(sqlite3.OperationalError, match='synthetic handle exception'):
        extract_llm.run_pending(led, limit=1, workers=1, budget_s=180,
            background=True, cloud_only=True, prioritize_patients=True, carry=[])
    assert _claim_count(led) == int(updater_changed)
    assert len(releases) == (0 if updater_changed else 2)
    assert not _current_artifacts(led) and calls == reservations == results == []
