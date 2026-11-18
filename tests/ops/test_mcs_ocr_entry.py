"""Synthetic OCR entry checks keep model work outside the ingestion lock."""
from types import SimpleNamespace

import pytest

import attachment_inference
import mcs_config
import mcs_ocr
import snapshots



@pytest.fixture(autouse=True)
def isolated_local_inference(monkeypatch):
    def pending(ledger, cfg, *, stage, deadline, limit):
        assert stage == "local" and deadline > 0 and limit == 1
        return {"done": 0, "deferred": 0}
    monkeypatch.setattr(attachment_inference, "run_pending", pending)

def test_disabled_does_not_open_or_probe_the_database(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled OCR accessed runtime or database")
    monkeypatch.setattr(mcs_ocr, "Ledger", forbidden)
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", forbidden)
    assert mcs_ocr.run({}) == {"reason": "disabled"}


def test_entry_releases_the_ingestion_lock_before_ocr(tmp_path, monkeypatch):
    database = tmp_path / "synthetic.db"
    database.touch()
    events = []
    cfg = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku"}}
    monkeypatch.setattr(mcs_ocr, "DB", str(database))
    monkeypatch.setattr(mcs_ocr, "DATA", str(tmp_path))
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", lambda: (False, "generation"))
    monkeypatch.setattr(mcs_ocr, "acquire_run_lock", lambda path: 123)
    monkeypatch.setattr(mcs_ocr.os, "close", lambda fd: events.append("unlock"))
    monkeypatch.setattr(mcs_ocr, "Ledger", lambda path: SimpleNamespace(close=lambda: events.append("close")))

    def engine(ledger, settings, *, deadline, limit):
        assert events == ["unlock"]
        assert settings is cfg and deadline > 0 and limit == 1
        events.append("ocr")
        return {"complete": 0}

    monkeypatch.setattr(mcs_ocr.attachment_ocr, "run_pending", engine)
    assert mcs_ocr.run(cfg) == {"complete": 0, "local_done": 0, "local_deferred": 0}
    assert events == ["unlock", "ocr", "close", "unlock"]


def test_ocr_config_uses_the_shared_runtime_validation():
    rule = mcs_config.CONFIG_RULES["attachment_ocr"][1]
    assert rule({"enabled": False}) is None
    assert rule({"enabled": "yes"})
    assert rule({"enabled": True, "executable": "relative"})
    assert rule({"enabled": True, "executable": "/synthetic/yomitoku", "timeout_seconds": float("nan")})


def test_cli_failure_is_nonzero_and_private_status_is_sanitized(tmp_path, monkeypatch, capsys):
    import json
    monkeypatch.setattr(mcs_ocr, "DATA", str(tmp_path))
    monkeypatch.setattr(mcs_ocr, "load_config", lambda: {})
    monkeypatch.setattr(mcs_ocr, "run", lambda cfg: {"complete": 0, "failed": 1})
    monkeypatch.setattr(mcs_ocr.sys, "argv", ["mcs_ocr", "--run"])
    assert mcs_ocr.main() == 1
    status = tmp_path / "attachment_ocr_status.json"
    assert status.stat().st_mode & 0o777 == 0o600
    assert set(json.loads(status.read_text())) == {"at", "complete", "failed"}
    assert json.loads(capsys.readouterr().out)["attachment_ocr"]["failed"] == 1


def test_configured_long_timeout_is_respected(tmp_path, monkeypatch):
    database = tmp_path / "synthetic.db"
    database.touch()
    cfg = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku", "timeout_seconds": 600}}
    monkeypatch.setattr(mcs_ocr, "DB", str(database))
    monkeypatch.setattr(mcs_ocr, "DATA", str(tmp_path))
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(mcs_ocr, "Ledger", lambda path: SimpleNamespace(close=lambda: None))
    now = mcs_ocr.time.monotonic()

    def engine(ledger, settings, *, deadline, limit):
        assert deadline - now >= 600
        return {"complete": 0}

    monkeypatch.setattr(mcs_ocr.attachment_ocr, "run_pending", engine)
    assert mcs_ocr.run(cfg) == {"complete": 0, "local_done": 0, "local_deferred": 0}


def test_saved_ocr_refreshes_snapshot_under_writer_lock(tmp_path, monkeypatch):
    import maintenance
    database = tmp_path / "synthetic.db"
    database.touch()
    cfg = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku"}}
    monkeypatch.setattr(mcs_ocr, "DB", str(database))
    monkeypatch.setattr(mcs_ocr, "DATA", str(tmp_path))
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(mcs_ocr.writer_lock, "wait_run_lock", lambda *args, **kwargs:
                        mcs_ocr.acquire_run_lock(str(tmp_path / "run.lock")))
    monkeypatch.setattr(mcs_ocr, "Ledger", lambda path: SimpleNamespace(close=lambda: None))
    pending = iter(({"complete": 1}, {"complete": 0}))
    monkeypatch.setattr(mcs_ocr.attachment_ocr, "run_pending", lambda *args, **kwargs: next(pending))
    calls = []

    def refresh(path, *, force, deadline):
        assert path == str(database) and force and deadline > 0
        assert mcs_ocr.acquire_run_lock(str(tmp_path / "run.lock")) is None
        calls.append(path)
        return "published"

    monkeypatch.setattr(maintenance, "refresh_snapshot", refresh)
    assert mcs_ocr.run(cfg) == {"complete": 1, "local_done": 0, "local_deferred": 0}
    assert calls == [str(database)]


def test_local_inference_runs_before_ocr_without_ingestion_lock(tmp_path, monkeypatch):
    database = tmp_path / "synthetic.db"
    database.touch()
    cfg = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku"}}
    monkeypatch.setattr(mcs_ocr, "DB", str(database))
    monkeypatch.setattr(mcs_ocr, "DATA", str(tmp_path))
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(mcs_ocr, "Ledger", lambda path: SimpleNamespace(close=lambda: None))
    events = []
    monkeypatch.setattr(mcs_ocr.attachment_ocr, "run_pending", lambda *args, **kwargs:
                        events.append("ocr") or {"complete": 0})

    def local(ledger, settings, *, stage, deadline, limit):
        assert events == [] and settings is cfg
        assert stage == "local" and deadline > mcs_ocr.time.monotonic() and limit == 1
        fd = mcs_ocr.acquire_run_lock(str(tmp_path / "run.lock"))
        assert fd is not None
        mcs_ocr.os.close(fd)
        return {"done": 0, "deferred": 1}

    monkeypatch.setattr(attachment_inference, "run_pending", local)
    assert mcs_ocr.run(cfg) == {"complete": 0, "local_done": 0, "local_deferred": 1,
                                "local_deferred_reasons": {"unclassified": 1}}


@pytest.mark.parametrize('phase', ['local', 'ocr', 'both'])
def test_drain_reports_captured_writer_phase_without_changing_counts(tmp_path, monkeypatch, phase):
    database = tmp_path / 'synthetic.db'
    database.touch()
    cfg = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/yomitoku'}}
    monkeypatch.setattr(mcs_ocr, 'DB', str(database))
    monkeypatch.setattr(mcs_ocr, 'DATA', str(tmp_path))
    monkeypatch.setattr(mcs_ocr.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(mcs_ocr, 'Ledger', lambda path: SimpleNamespace(close=lambda: None))

    def refuse():
        assert mcs_ocr.writer_lock.wait_run_lock(str(tmp_path / 'run.lock'), wait_s=0,
            expected_code_stamp=1, marker_path=str(tmp_path / 'marker')) is None

    def local(*args, **kwargs):
        if phase in ('local', 'both'):
            refuse()
        return {'done': 0, 'deferred': int(phase in ('local', 'both'))}

    def ocr(*args, **kwargs):
        if phase in ('ocr', 'both'):
            refuse()
        return {'complete': 0, 'deferred': int(phase in ('ocr', 'both'))}

    monkeypatch.setattr(attachment_inference, 'run_pending', local)
    monkeypatch.setattr(mcs_ocr.attachment_ocr, 'run_pending', ocr)
    assert mcs_ocr.run(cfg) == {'complete': 0, 'local_done': 0,
        'local_deferred': int(phase in ('local', 'both')), 'deferred': int(phase in ('ocr', 'both')),
        **({'local_deferred_reasons': {'writer_refused': 1}} if phase != 'ocr' else {}),
        'writer_reason': 'run_lock_not_acquired', 'writer_stage': 'write_lock',
        'writer_job_stage': 'local' if phase == 'local' else 'ocr'}


def test_local_inference_deadline_uses_the_extended_page_budget(tmp_path, monkeypatch):
    database = tmp_path / "synthetic.db"
    database.touch()
    cfg = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku"}}
    monkeypatch.setattr(mcs_ocr, "DB", str(database))
    monkeypatch.setattr(mcs_ocr, "DATA", str(tmp_path))
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(mcs_ocr, "Ledger", lambda path: SimpleNamespace(close=lambda: None))
    now = [1000.0]
    monkeypatch.setattr(mcs_ocr.time, "monotonic", lambda: now[0])
    received = []

    def local(ledger, settings, *, stage, deadline, limit):
        assert stage == "local" and limit == 1
        received.append(deadline - now[0])
        return {"done": 0, "deferred": 1}

    monkeypatch.setattr(attachment_inference, "run_pending", local)
    monkeypatch.setattr(mcs_ocr.attachment_ocr, "run_pending",
                        lambda *args, **kwargs: {"complete": 0})
    mcs_ocr.run(cfg)
    assert mcs_ocr.LOCAL_CALL_S == 300
    assert received == [mcs_ocr.LOCAL_CALL_S]


def test_local_inference_deadline_never_exceeds_the_remaining_job_budget(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(mcs_ocr.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", lambda: (False, 1))
    received = []

    def local(ledger, settings, *, stage, deadline, limit):
        received.append(deadline - now[0])
        return {"done": 0, "deferred": 1}

    monkeypatch.setattr(attachment_inference, "run_pending", local)
    monkeypatch.setattr(mcs_ocr.attachment_ocr, "run_pending",
                        lambda *args, **kwargs: {"complete": 0})
    cfg = {"attachment_ocr": {"enabled": True, "timeout_seconds": 120}}
    result = mcs_ocr._drain(SimpleNamespace(), cfg, (False, 1), now[0] + 120,
                            live_config=False)
    assert received == [115]
    assert result["local_deferred"] == 1
    assert result["local_deferred_reasons"] == {"unclassified": 1}


def test_large_raw_timeout_runs_original_ocr_before_local_inference(tmp_path, monkeypatch):
    database = tmp_path / "synthetic.db"
    database.touch()
    cfg = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku",
                              "timeout_seconds": 600}}
    monkeypatch.setattr(mcs_ocr, "DB", str(database))
    monkeypatch.setattr(mcs_ocr, "DATA", str(tmp_path))
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(mcs_ocr, "Ledger", lambda path: SimpleNamespace(close=lambda: None))
    now = [1000.0]
    monkeypatch.setattr(mcs_ocr.time, "monotonic", lambda: now[0])
    events = []

    def local(ledger, settings, *, stage, deadline, limit):
        events.append("local")
        return {"done": 0, "deferred": 0}

    def ocr(ledger, settings, *, deadline, limit):
        events.append("ocr")
        return {"complete": 0}

    monkeypatch.setattr(attachment_inference, "run_pending", local)
    monkeypatch.setattr(mcs_ocr.attachment_ocr, "run_pending", ocr)
    mcs_ocr.run(cfg)
    assert events == ["ocr", "local"]


def test_ocr_call_deadline_is_the_job_residual_not_runtime_window(tmp_path, monkeypatch):
    database = tmp_path / 'synthetic.db'
    database.touch()
    cfg = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/yomitoku'}}
    monkeypatch.setattr(mcs_ocr, 'DB', str(database))
    monkeypatch.setattr(mcs_ocr, 'DATA', str(tmp_path))
    monkeypatch.setattr(mcs_ocr.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(mcs_ocr, 'Ledger', lambda path: SimpleNamespace(close=lambda: None))
    now = [1000.0]
    monkeypatch.setattr(mcs_ocr.time, 'monotonic', lambda: now[0])
    received = []
    monkeypatch.setattr(attachment_inference, 'run_pending',
                        lambda *args, **kwargs: {'done': 0, 'deferred': 0})
    monkeypatch.setattr(mcs_ocr.attachment_ocr, 'run_pending',
                        lambda *args, **kwargs: received.append(kwargs['deadline']) or {'complete': 0})
    mcs_ocr.run(cfg)
    assert len(received) == 1
    work_deadline = snapshots.snapshot_work_deadline(
        now[0] + mcs_ocr.DRAIN_BUDGET_S, str(database))
    assert received == [work_deadline - 5]


@pytest.mark.parametrize("outcome", ["ready", "busy", "updated", "deadline", "cancelled"])
def test_startup_wait_uses_existing_job_budget_and_writer_fence(tmp_path, monkeypatch, outcome):
    database = tmp_path / "synthetic.db"
    database.touch()
    cfg = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku"}}
    monkeypatch.setattr(mcs_ocr, "DB", str(database))
    monkeypatch.setattr(mcs_ocr, "DATA", str(tmp_path))
    now = [100.0]
    generation = [(False, 7)]
    monkeypatch.setattr(mcs_ocr.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", lambda: generation[0])
    closed, waits, opened, drained = [], [], [], []
    work_deadline = snapshots.snapshot_work_deadline(
        now[0] + mcs_ocr.DRAIN_BUDGET_S, str(database))
    locks = iter((101, None))
    monkeypatch.setattr(mcs_ocr, "acquire_run_lock", lambda path: next(locks))
    monkeypatch.setattr(mcs_ocr.os, "close", closed.append)
    cancellation = KeyboardInterrupt("synthetic cancellation")

    def wait(path, *, wait_s, expected_code_stamp, marker_path):
        waits.append(path)
        assert path == str(tmp_path / "run.lock")
        assert wait_s == work_deadline - now[0] - 5
        assert expected_code_stamp == 7
        assert marker_path == str(tmp_path / "update_in_progress.marker")
        if outcome == "cancelled":
            raise cancellation
        if outcome == "busy":
            return None
        now[0] += 20 if outcome != "deadline" else mcs_ocr.DRAIN_BUDGET_S
        if outcome == "updated":
            generation[0] = (False, 8)
        return 102

    monkeypatch.setattr(mcs_ocr.writer_lock, "wait_run_lock", wait)

    def ledger(path):
        assert closed == []
        opened.append(path)
        return SimpleNamespace(close=lambda: closed.append("ledger"))

    def drain(ledger, settings, state, deadline, *, live_config):
        assert closed == [102]
        assert settings is cfg and state == (False, 7) and live_config is False
        assert deadline == work_deadline
        drained.append(deadline)
        return {"local_done": 0, "local_deferred": 0}

    monkeypatch.setattr(mcs_ocr, "Ledger", ledger)
    monkeypatch.setattr(mcs_ocr, "_drain", drain)
    if outcome == "cancelled":
        with pytest.raises(KeyboardInterrupt) as caught:
            mcs_ocr.run(cfg)
        assert caught.value is cancellation
    else:
        expected = ({"local_done": 0, "local_deferred": 0} if outcome == "ready" else
                    {"reason": "writer_blocked" if outcome == "updated" else "writer_busy"})
        assert mcs_ocr.run(cfg) == expected
    assert waits == [str(tmp_path / "run.lock")]
    assert len(opened) == len(drained) == int(outcome == "ready")
    assert closed == ([102, "ledger", 101] if outcome == "ready" else
                      [102, 101] if outcome in ("updated", "deadline") else [101])


def test_cli_explicit_retry_errors_reaches_only_the_bounded_job(tmp_path, monkeypatch, capsys):
    calls = []
    def run(cfg, *, retry_errors=False):
        calls.append((cfg, retry_errors))
        return {'reason': 'disabled'}
    monkeypatch.setattr(mcs_ocr, 'DATA', str(tmp_path))
    monkeypatch.setattr(mcs_ocr, 'run', run)
    monkeypatch.setattr(mcs_ocr.sys, 'argv', ['mcs_ocr', '--run', '--retry-errors'])
    assert mcs_ocr.main() == 0 and calls == [(None, True)]
    assert 'disabled' in capsys.readouterr().out


def _probe_ledger(tmp_path):
    import sqlite3
    return SimpleNamespace(db=sqlite3.connect(str(tmp_path / "synthetic.db")), close=lambda: None)


@pytest.fixture
def probe(tmp_path, monkeypatch):
    import cloud_backlog
    import extract_llm
    state = {"free": 10_000.0, "floor": 100.0, "headroom": 1 << 30}
    monkeypatch.setattr(extract_llm, "disk_free_mb", lambda db: state["free"])
    monkeypatch.setattr(extract_llm, "disk_floor_mb", lambda: state["floor"])
    monkeypatch.setattr(cloud_backlog, "memory_headroom", lambda: state["headroom"])
    return state


@pytest.mark.parametrize("change,code", [
    ({}, "unclassified"),
    ({"free": None}, "disk_unknown"),
    ({"free": 99.0}, "disk_low"),
    ({"headroom": None}, "memory_unknown"),
    ({"headroom": -1}, "memory_low"),
    ({"free": 99.0, "headroom": None}, "disk_low"),
])
def test_local_deferral_reason_reobserves_admission_conditions(tmp_path, probe, change, code):
    probe.update(change)
    ledger = _probe_ledger(tmp_path)
    assert mcs_ocr._local_defer_reason(ledger, None, 0.1) == code
    assert mcs_ocr._local_defer_reason(
        ledger, {"writer_reason": "writer_refusal_unclassified", "writer_stage": "writer_guard"}, 0.1) == code


def test_local_deferral_reason_names_a_held_inference_lock_and_releases_its_probe(tmp_path, probe):
    ledger = _probe_ledger(tmp_path)
    path = str(tmp_path / "attachment_inference_local.lock")
    held = mcs_ocr.writer_lock.acquire_run_lock(path)
    try:
        assert mcs_ocr._local_defer_reason(ledger, None, 0.1) == "inference_lock_busy"
    finally:
        mcs_ocr.os.close(held)
    assert mcs_ocr._local_defer_reason(ledger, None, 0.1) == "unclassified"
    again = mcs_ocr.writer_lock.acquire_run_lock(path)
    assert again is not None
    mcs_ocr.os.close(again)


def test_local_deferral_reason_prefers_the_recorded_writer_refusal(tmp_path, probe):
    probe.update(free=None)
    diagnostic = {"writer_reason": "run_lock_not_acquired", "writer_stage": "write_lock"}
    assert mcs_ocr._local_defer_reason(_probe_ledger(tmp_path), diagnostic, 299.0) == "writer_refused"


@pytest.mark.parametrize("elapsed_s", [mcs_ocr.ADMISSION_WINDOW_S + 0.1, 299.0, -1.0, None])
def test_local_deferral_after_a_long_page_run_is_not_attributed_to_a_later_recheck(tmp_path, probe, elapsed_s):
    # A deferral after the LLM call (quality hold, no result, save race) must
    # not be named after whatever the re-check happens to see afterwards.
    probe.update(headroom=-1)
    assert mcs_ocr._local_defer_reason(_probe_ledger(tmp_path), None, elapsed_s) == "unclassified"
    assert mcs_ocr._local_defer_reason(_probe_ledger(tmp_path), None, mcs_ocr.ADMISSION_WINDOW_S) == "memory_low"


def test_drain_passes_the_page_run_duration_to_the_classifier(monkeypatch):
    now = [1000.0]
    seen = []
    monkeypatch.setattr(mcs_ocr.time, "monotonic", lambda: now[0])

    def local(*args, **kwargs):
        now[0] += 120.0
        return {"done": 0, "deferred": 1}

    def classify(ledger, diagnostic, elapsed_s):
        seen.append(elapsed_s)
        return "unclassified"

    monkeypatch.setattr(mcs_ocr, "_local_defer_reason", classify)
    _drain_with(monkeypatch, local)
    assert seen == [120.0]


def test_local_deferral_observation_failure_is_unclassified_and_keeps_the_outcome(tmp_path, monkeypatch):
    import extract_llm

    def broken(db):
        raise RuntimeError("synthetic secret-like detail sk-SYNTHETIC")

    monkeypatch.setattr(extract_llm, "disk_free_mb", broken)
    assert mcs_ocr._local_defer_reason(_probe_ledger(tmp_path), None, 0.1) == "unclassified"


def _drain_with(monkeypatch, local):
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(attachment_inference, "run_pending", local)
    monkeypatch.setattr(mcs_ocr.attachment_ocr, "run_pending", lambda *args, **kwargs: {"complete": 0})
    cfg = {"attachment_ocr": {"enabled": True, "timeout_seconds": 120}}
    return mcs_ocr._drain(SimpleNamespace(), cfg, (False, 1), mcs_ocr.time.monotonic() + 600,
                          live_config=False)


def test_drain_records_only_fixed_reason_counts_for_local_deferrals(monkeypatch):
    monkeypatch.setattr(mcs_ocr, "_local_defer_reason", lambda ledger, diagnostic, elapsed_s: "disk_low")
    result = _drain_with(monkeypatch, lambda *args, **kwargs: {"done": 0, "deferred": 1})
    assert result["local_deferred"] == 1
    assert result["local_deferred_reasons"] == {"disk_low": 1}


def test_drain_unknown_reason_code_becomes_unclassified(monkeypatch):
    monkeypatch.setattr(mcs_ocr, "_local_defer_reason", lambda ledger, diagnostic, elapsed_s: "synthetic_free_text")
    result = _drain_with(monkeypatch, lambda *args, **kwargs: {"done": 0, "deferred": 1})
    assert result["local_deferred_reasons"] == {"unclassified": 1}


def test_drain_local_stage_exception_is_counted_without_its_text(monkeypatch):
    def local(*args, **kwargs):
        raise ValueError("synthetic secret-like detail /private/path sk-SYNTHETIC")

    result = _drain_with(monkeypatch, local)
    assert result["reason"] == "local_processing_failed" and result["local_deferred"] == 1
    assert result["local_deferred_reasons"] == {"local_stage_error": 1}
    assert "SYNTHETIC" not in mcs_ocr.json.dumps(result) and "private" not in mcs_ocr.json.dumps(result)


def test_drain_without_local_deferral_has_no_breakdown(monkeypatch):
    result = _drain_with(monkeypatch, lambda *args, **kwargs: {"done": 0, "deferred": 0})
    assert result["local_deferred"] == 0 and "local_deferred_reasons" not in result


def test_drain_early_return_still_normalizes_the_breakdown(monkeypatch):
    states = iter([(False, 1)] + [(True, 2)] * 10)
    monkeypatch.setattr(mcs_ocr, "_local_defer_reason", lambda ledger, diagnostic, elapsed_s: "memory_low")
    monkeypatch.setattr(attachment_inference, "run_pending", lambda *args, **kwargs: {"done": 0, "deferred": 1})
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", lambda: next(states))
    cfg = {"attachment_ocr": {"enabled": True, "timeout_seconds": 120}}
    result = mcs_ocr._drain(SimpleNamespace(), cfg, (False, 1), mcs_ocr.time.monotonic() + 600,
                            live_config=False)
    assert result["reason"] == "writer_blocked"
    assert result["local_deferred_reasons"] == {"memory_low": 1}


def test_status_breakdown_yields_to_the_reader_size_limit():
    small = {"local_deferred": 1, "local_deferred_reasons": {"disk_low": 1}}
    assert mcs_ocr.json.loads(mcs_ocr._status_text(small, 1000.0))["local_deferred_reasons"] == {"disk_low": 1}
    block = {f"synthetic_key_{index:02d}": f"synthetic_value_{index:02d}" * 4 for index in range(16)}
    large = {**small, "snapshot_failure": block, "snapshot_timing": block,
             "failure_reason_counts": {f"synthetic_reason_{index:03d}": 1 for index in range(60)}}
    text = mcs_ocr._status_text(large, 1000.0)
    assert "local_deferred_reasons" not in mcs_ocr.json.loads(text)
    assert mcs_ocr.json.loads(text)["snapshot_failure"] == block


def test_cli_status_file_carries_a_breakdown_the_health_reader_accepts(tmp_path, monkeypatch):
    import health_report
    monkeypatch.setattr(mcs_ocr, "DATA", str(tmp_path))
    monkeypatch.setattr(mcs_ocr.sys, "argv", ["mcs_ocr", "--run"])
    monkeypatch.setattr(mcs_ocr, "run", lambda cfg: {
        "complete": 0, "empty": 0, "failed": 0, "unsupported": 0, "deferred": 0, "pending_failed": 0,
        "queue_status": "known", "local_done": 0, "local_deferred": 2,
        "local_deferred_reasons": {"memory_unknown": 2}})
    assert mcs_ocr.main() == 1
    cfg = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku"}}
    at = mcs_ocr.json.loads((tmp_path / "attachment_ocr_status.json").read_text())["at"]
    report = health_report.attachment_ocr_health(str(tmp_path), cfg, at + 1)
    assert report["reason"] == "attachment_ocr_local_inference_deferred"
    assert report["local_deferred_reasons"] == {"memory_unknown": 2}
    assert report["local_deferred_primary"] == "memory_unknown"


def test_live_restart_intent_yields_before_opening_the_database(tmp_path, monkeypatch):
    db = tmp_path / "chat.db"
    db.touch()
    cfg = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku", "db": str(db)}}
    monkeypatch.setattr(mcs_ocr, "DB", str(db))
    monkeypatch.setattr(mcs_ocr, "DATA", str(tmp_path))
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(mcs_ocr, "acquire_run_lock",
                        lambda path: object() if "attachment_ocr" in path else None)
    monkeypatch.setattr(mcs_ocr.writer_lock, "wait_run_lock", lambda *a, **k: None)
    monkeypatch.setattr(mcs_ocr.writer_lock, "restart_intent_active", lambda *a, **k: True)
    class ForbiddenLedger:
        def __init__(self, *a, **k):
            raise AssertionError("db must not open")
    monkeypatch.setattr(mcs_ocr, "Ledger", ForbiddenLedger)
    assert mcs_ocr.run(cfg) == {"reason": "restart_yield"}
