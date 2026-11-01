"""Cloud selection progresses past unreceipted replies without widening history scope."""
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
    assert report["done"] == 0 and not led.artifacts(extract_llm.patient_state_context.KIND)
    assert calls == reservations == results == [] and _claim_count(led) == 0


def test_incomplete_extract_selection_preserves_carry_and_unknown_counts(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    clock = [time.monotonic()]
    deadline = clock[0] + 180
    carry = [object()]
    observed = []

    def incomplete(*args, **kwargs):
        observed.append(kwargs.get("deadline"))
        raise extract_llm._CloudSelectionIncomplete("deadline")

    monkeypatch.setattr(extract_llm, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(extract_llm, "_select_pending", incomplete)
    monkeypatch.setattr(extract_llm, "_prepare_jobs", lambda *a, **kw: pytest.fail("incomplete selection prepared work"))
    report = extract_llm.run_pending(led, limit=1, budget_s=180, cloud_only=True, carry=carry)
    assert observed == [deadline] and report["carry"] is carry
    assert report["cloud_selection_incomplete"] is True and report["cloud_selection_reason"] == "deadline"
    assert report["selected"] is report["fresh_selected"] is report["left"] is report["left_estimate"] is None
    assert report["left_estimate_age_s"] is None and report["queue_ages_s"] == {"oldest": None, "newest": None}
    assert report["done"] == 0 and calls == reservations == results == [] and _claim_count(led) == 0


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
    assert [row["message_id"] for row in led.artifacts(extract_llm.patient_state_context.KIND)] == [
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
            assert {r["message_id"] for r in led.artifacts(extract_llm.patient_state_context.KIND)} == {
                row["message_id"] for row in selected[:2]}
        finally:
            if competing:
                release(other, *competing[0])
        assert _cloud_pass(other)["done"] == 1
    assert len(calls) == len(reservations) == len(results) == 2
    assert len({r["identity"]["message_id"] for r in reservations}) == 2
    assert len(_current_artifacts(led)) == 2 and _claim_count(led) == 0


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
        assert len(led.artifacts(extract_llm.patient_state_context.KIND)) == 2
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
