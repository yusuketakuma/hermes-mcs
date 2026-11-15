"""Synthetic batch timing preserves completion, deferral and exception contracts."""
from types import SimpleNamespace

import pytest

import rollup


def test_batch_timing_includes_interrupted_attempts_and_counts_only_completed(monkeypatch):
    clock = [10.0]
    calls, changed, unknown, reasons, timings = [], [], [], {}, {}
    monkeypatch.setattr(rollup, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(rollup.rollup_builder, "_check_deadline", lambda deadline: None)

    def rebuild(ledger, pid, **kwargs):
        calls.append(pid)
        clock[0] += pid + 0.12345
        if pid == 2:
            raise rollup.RollupSourceChanged()
        if pid == 3:
            raise rollup.RollupSourceUnverified()
        if pid == 4:
            raise rollup.RollupDeadlineExceeded()

    monkeypatch.setattr(rollup, "rebuild", rebuild)
    assert rollup.rebuild_many(None, [1, 2, 3, 4, 5], deadline=100,
                               source_changed=changed, source_unknown=unknown,
                               source_unknown_reasons=reasons, timings=timings) == 1
    assert calls == [1, 2, 3, 4] and changed == [2] and unknown == [3, 4, 5]
    assert reasons == {"source_unverified": 1, "deadline": 2}
    assert timings == {"rebuilt": 1, "deferred": 4, "max_patient_s": 4.123}


@pytest.mark.parametrize("error", [RuntimeError("synthetic-private"), KeyboardInterrupt()])
def test_batch_exception_keeps_partial_counts_and_original_exception(monkeypatch, error):
    clock = [0.0]
    timings = {}
    monkeypatch.setattr(rollup, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def rebuild(ledger, pid, **kwargs):
        clock[0] += pid
        if pid == 2:
            raise error

    monkeypatch.setattr(rollup, "rebuild", rebuild)
    with pytest.raises(type(error)) as caught:
        rollup.rebuild_many(None, [1, 2, 3], timings=timings)
    assert caught.value is error
    assert timings == {"rebuilt": 1, "deferred": 2, "max_patient_s": 2.0}
    assert "synthetic-private" not in str(timings)


@pytest.mark.parametrize("failure_at", [1, 2])
def test_timing_clock_failure_omits_maximum_without_changing_completion(monkeypatch, failure_at):
    calls = []

    def clock():
        calls.append(True)
        if len(calls) == failure_at:
            raise OSError("synthetic-private")
        return 1.0

    monkeypatch.setattr(rollup, "time", SimpleNamespace(monotonic=clock))
    monkeypatch.setattr(rollup, "rebuild", lambda *a, **k: None)
    timings = {}
    assert rollup.rebuild_many(None, [1, 2], timings=timings) == 2
    assert timings == {"rebuilt": 2, "deferred": 0}


def test_pre_attempt_deadline_and_empty_batch_have_no_patient_time(monkeypatch):
    def expired(deadline):
        raise rollup.RollupDeadlineExceeded()

    monkeypatch.setattr(rollup.rollup_builder, "_check_deadline", expired)
    monkeypatch.setattr(rollup, "time", SimpleNamespace(
        monotonic=lambda: pytest.fail("no rebuild attempt has started")))
    unknown, timings = [], {}
    assert rollup.rebuild_many(None, [1, 2], deadline=0, source_unknown=unknown,
                               timings=timings) == 0
    assert unknown == [1, 2]
    assert timings == {"rebuilt": 0, "deferred": 2, "max_patient_s": 0.0}
    assert rollup.rebuild_many(None, [], timings=timings) == 0
    assert timings == {"rebuilt": 0, "deferred": 0, "max_patient_s": 0.0}


def test_callers_without_timing_collector_do_not_read_an_extra_clock(monkeypatch):
    monkeypatch.setattr(rollup, "time", SimpleNamespace(
        monotonic=lambda: pytest.fail("timing was not requested")))
    monkeypatch.setattr(rollup, "rebuild", lambda *a, **k: None)
    assert rollup.rebuild_many(None, [1, 2]) == 2
