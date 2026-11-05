"""Synthetic cloud selection progress contracts with isolated shared fixtures."""
import extract_llm_carry
import extract_llm_context
import extract_llm_engine
import extract_llm_errors
import extract_llm_prepare
import extract_llm_provider
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
from contextlib import closing
import sqlite3
import time
from types import SimpleNamespace
import pytest
import cloud_policy
import extract_llm
import cloud_inference
import semantic_drain
from semantic_testkit import _ledger
from test_anthropic_runtime import _assert_legacy_retained as _assert_legacy_retained, _current_artifacts as _current_artifacts, old as old, routed as routed
from cloud_selection_progress_testkit import _claim_count as _claim_count, _make_paid_carry as _make_paid_carry, _select as _select, blocked_prefix as blocked_prefix, cloud_candidates as cloud_candidates


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
    module = semantic_drain if lane == "semantic" else extract_llm
    for _ in range(4):
        try:
            selected = _select(led, lane, deadline=time.monotonic() + 60)
            break
        except module._CloudSelectionIncomplete as error:
            # A bounded SQL window may end on a slower host. Resume the same
            # proof-bound selection next pass; never turn it into empty success.
            assert str(error) == "deadline"
    else:
        pytest.fail("bounded selection did not advance to the receipted tail")
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
        monkeypatch.setattr(extract_llm_selection, '_select_pending_impl', extract_llm._select_pending_impl)
    expected = primary or extract_llm._CloudSelectionIncomplete
    reason = "synthetic-primary" if primary is not None else "reader_close_failed"
    with pytest.raises(expected, match="^" + reason + "$"):
        _select(led, "extract", deadline=time.monotonic() + 60)
    assert closed == [True] and led.db.execute("SELECT 1").fetchone()[0] == 1


def test_selection_rejects_a_replaced_reader_file(cloud_candidates, monkeypatch):
    led, *_ = cloud_candidates
    identities = iter([SimpleNamespace(st_dev=1, st_ino=1), SimpleNamespace(st_dev=1, st_ino=2)])
    monkeypatch.setattr(extract_llm, "os", SimpleNamespace(stat=lambda _: next(identities)))
    monkeypatch.setattr(extract_llm_errors, 'os', extract_llm.os)
    monkeypatch.setattr(extract_llm_prepare, 'os', extract_llm.os)
    monkeypatch.setattr(extract_llm_resident, 'os', extract_llm.os)
    monkeypatch.setattr(extract_llm_runner, 'os', extract_llm.os)
    monkeypatch.setattr(extract_llm_selection, 'os', extract_llm.os)
    monkeypatch.setattr(extract_llm_source_contract, 'os', extract_llm.os)
    monkeypatch.setattr(extract_llm_store, 'os', extract_llm.os)
    with pytest.raises(extract_llm._CloudSelectionIncomplete, match="^reader_changed$"):
        _select(led, "extract", deadline=time.monotonic() + 60)
    assert led.db.execute("SELECT 1").fetchone()[0] == 1


def test_retained_extract_pass_skips_selection_and_preserves_unknown_counts(cloud_candidates, monkeypatch):
    led, carry, _, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    clock = [time.monotonic()]
    observed = []

    def incomplete(*args, **kwargs):
        observed.append(kwargs.get("deadline"))
        raise extract_llm._CloudSelectionIncomplete("deadline")

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
    monkeypatch.setattr(extract_llm, "_select_pending", incomplete)
    monkeypatch.setattr(extract_llm_runner, '_select_pending', extract_llm._select_pending)
    monkeypatch.setattr(extract_llm, "_prepare_jobs", lambda *a, **kw: pytest.fail("incomplete selection prepared work"))
    monkeypatch.setattr(extract_llm_runner, '_prepare_jobs', extract_llm._prepare_jobs)
    report = extract_llm.run_pending(led, limit=1, budget_s=180, cloud_only=True,
                                     per_write_lock=True, carry=carry)
    assert observed == [] and report["carry"] is carry
    assert report["carry_waiting"] is True and "cloud_selection_incomplete" not in report
    assert report["selected"] == report["fresh_selected"] == 0
    assert report["left"] is report["left_estimate"] is None
    assert report["left_estimate_age_s"] is None and report["queue_ages_s"] == {"oldest": None, "newest": None}
    assert report["done"] == 0 and len(calls) == len(reservations) == len(results) == 1
    assert len(carry) == 1 and _claim_count(led) == 1


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
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', extract_llm.acquire_run_lock)
    monkeypatch.setattr(extract_llm_resident, 'acquire_run_lock', extract_llm.acquire_run_lock)
    monkeypatch.setattr(extract_llm, '_writer_current', lambda **kw: True)
    monkeypatch.setattr(extract_llm_carry, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_errors, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_resident, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_runner, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_selection, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_source_contract, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
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
