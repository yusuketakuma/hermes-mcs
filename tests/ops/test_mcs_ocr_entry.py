"""Synthetic OCR entry checks keep model work outside the ingestion lock."""
from types import SimpleNamespace

import pytest

import attachment_inference
import mcs_config
import mcs_ocr



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
    assert mcs_ocr.run(cfg) == {"complete": 0, "local_done": 0, "local_deferred": 1}


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
    received = []

    def local(ledger, settings, *, stage, deadline, limit):
        assert stage == "local" and limit == 1
        received.append(deadline - mcs_ocr.time.monotonic())
        return {"done": 0, "deferred": 1}

    monkeypatch.setattr(attachment_inference, "run_pending", local)
    monkeypatch.setattr(mcs_ocr.attachment_ocr, "run_pending",
                        lambda *args, **kwargs: {"complete": 0})
    mcs_ocr.run(cfg)
    assert mcs_ocr.LOCAL_CALL_S == 300
    assert received and all(0 < span <= mcs_ocr.LOCAL_CALL_S for span in received)


def test_ocr_call_deadline_is_the_job_residual_not_runtime_window(tmp_path, monkeypatch):
    database = tmp_path / 'synthetic.db'
    database.touch()
    cfg = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/yomitoku'}}
    monkeypatch.setattr(mcs_ocr, 'DB', str(database))
    monkeypatch.setattr(mcs_ocr, 'DATA', str(tmp_path))
    monkeypatch.setattr(mcs_ocr.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(mcs_ocr, 'Ledger', lambda path: SimpleNamespace(close=lambda: None))
    received = []
    monkeypatch.setattr(attachment_inference, 'run_pending',
                        lambda *args, **kwargs: {'done': 0, 'deferred': 0})
    monkeypatch.setattr(mcs_ocr.attachment_ocr, 'run_pending',
                        lambda *args, **kwargs: received.append(kwargs['deadline']) or {'complete': 0})
    mcs_ocr.run(cfg)
    assert len(received) == 1
    assert received[0] - mcs_ocr.time.monotonic() > mcs_ocr.DRAIN_BUDGET_S - 6


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
    locks = iter((101, None))
    monkeypatch.setattr(mcs_ocr, "acquire_run_lock", lambda path: next(locks))
    monkeypatch.setattr(mcs_ocr.os, "close", closed.append)
    cancellation = KeyboardInterrupt("synthetic cancellation")

    def wait(path, *, wait_s, expected_code_stamp, marker_path):
        waits.append(path)
        assert path == str(tmp_path / "run.lock")
        assert wait_s == mcs_ocr.DRAIN_BUDGET_S - 5
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
        assert deadline == 100 + mcs_ocr.DRAIN_BUDGET_S
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
