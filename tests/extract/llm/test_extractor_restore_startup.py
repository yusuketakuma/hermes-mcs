"""Restore holds stop the extractor before opening a synthetic writer ledger."""
import extract_llm_carry
import extract_llm_engine
import extract_llm_prepare
import extract_llm_provider
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import extract_llm
import writer_lock


@pytest.fixture
def root(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(writer_lock, "HOME", str(tmp_path))
    monkeypatch.setattr(writer_lock, "RUN_LOCK", str(data / "run.lock"))
    monkeypatch.setattr(writer_lock, "code_stamp", lambda: extract_llm._WRITER_STATE[1])
    monkeypatch.setattr(extract_llm, "HOME", str(tmp_path))
    monkeypatch.setattr(extract_llm_engine, 'HOME', extract_llm.HOME)
    monkeypatch.setattr(extract_llm_prepare, 'HOME', extract_llm.HOME)
    monkeypatch.setattr(extract_llm_provider, 'HOME', extract_llm.HOME)
    monkeypatch.setattr(extract_llm_resident, 'HOME', extract_llm.HOME)
    monkeypatch.setattr(extract_llm_selection, 'HOME', extract_llm.HOME)
    monkeypatch.setattr(extract_llm_source_contract, 'HOME', extract_llm.HOME)
    monkeypatch.setattr(extract_llm_store, 'HOME', extract_llm.HOME)
    return data


@pytest.mark.parametrize("args", [[], ["--revive-failed"], ["--all", "--stop-after", "1"]])
@pytest.mark.parametrize("marker", [
    {"phase": "awaiting_consent", "by": "mcs_update"},
    {"phase": "restored", "by": "mcs_recover"},
    "broken-json",
])
def test_restore_hold_rejects_before_ledger(root, monkeypatch, capsys, args, marker):
    (root / "restore_pending.json").write_text(
        marker if isinstance(marker, str) else json.dumps(marker))
    monkeypatch.setattr(extract_llm.sys, "argv", ["extract_llm", *args])
    monkeypatch.setattr(extract_llm, "Ledger", lambda *_: pytest.fail("opened writer during restore"))
    monkeypatch.setattr(extract_llm_resident, 'Ledger', extract_llm.Ledger)
    assert extract_llm.main() == 1
    assert json.loads(capsys.readouterr().out)["error"] == "writer_unavailable"
    fd = writer_lock.acquire_run_lock()
    assert fd is not None
    os.close(fd)


def test_constructor_is_locked_and_lock_is_released(root, monkeypatch):
    ledger = object()
    def construct(_):
        assert writer_lock.acquire_run_lock() is None
        return ledger
    monkeypatch.setattr(extract_llm, "Ledger", construct)
    monkeypatch.setattr(extract_llm_resident, 'Ledger', extract_llm.Ledger)
    assert extract_llm._open_current_ledger() is ledger
    fd = writer_lock.acquire_run_lock()
    assert fd is not None
    os.close(fd)


def test_restore_appearing_during_acquire_prevents_constructor(root, monkeypatch):
    acquire = writer_lock.acquire_run_lock
    def acquire_then_hold():
        fd = acquire()
        (root / "restore_pending.json").write_text(json.dumps(
            {"phase": "restored", "by": "mcs_update"}))
        return fd
    monkeypatch.setattr(writer_lock, "acquire_run_lock", acquire_then_hold)
    monkeypatch.setattr(extract_llm, "Ledger", lambda *_: pytest.fail("opened stale writer"))
    monkeypatch.setattr(extract_llm_resident, 'Ledger', extract_llm.Ledger)
    assert extract_llm._open_current_ledger() is None
    fd = acquire()
    assert fd is not None
    os.close(fd)


def test_busy_writer_never_opens_ledger(root, monkeypatch):
    fd = writer_lock.acquire_run_lock()
    assert fd is not None
    monkeypatch.setattr(extract_llm, "Ledger", lambda *_: pytest.fail("opened unlocked writer"))
    monkeypatch.setattr(extract_llm_resident, 'Ledger', extract_llm.Ledger)
    try:
        assert extract_llm._open_current_ledger() is None
    finally:
        os.close(fd)


@pytest.mark.parametrize("during_lock", [False, True])
def test_restore_hold_keeps_paid_carry_without_writes(root, monkeypatch, during_lock):
    def hold():
        (root / "restore_pending.json").write_text(json.dumps(
            {"phase": "restored", "by": "mcs_update"}))
    @contextmanager
    def locked():
        if during_lock:
            hold()
        yield True
    if not during_lock:
        hold()
    def forbidden(*args, **kwargs):
        pytest.fail("restore hold must retain paid evidence without writes")
    carry = [({"_cloud_discard": forbidden}, 0.0, forbidden)]
    monkeypatch.setattr(extract_llm, "_release", forbidden)
    monkeypatch.setattr(extract_llm_carry, '_release', extract_llm._release)
    monkeypatch.setattr(extract_llm_runner, '_release', extract_llm._release)
    monkeypatch.setattr(extract_llm, "_renew", forbidden)
    monkeypatch.setattr(extract_llm_carry, '_renew', extract_llm._renew)
    monkeypatch.setattr(extract_llm_runner, '_renew', extract_llm._renew)
    result = extract_llm._settle_carry(object(), carry, write=False, lock_context=locked())
    assert result == ({"done": 0, "held_prior": 0, "deferred": 0}, 0, set())
    assert len(carry) == 1


def test_run_pending_returns_existing_carry_while_writer_is_blocked(root, monkeypatch):
    (root / "restore_pending.json").write_text(json.dumps(
        {"phase": "awaiting_consent", "by": "mcs_update"}))
    carry = [(object(), 0.0, object())]
    ledger = SimpleNamespace(db=SimpleNamespace(in_transaction=False))
    out = extract_llm.run_pending(ledger, carry=carry)
    assert out["updated"] is True and out["done"] == 0
    assert out["carry"] is carry


def test_revive_cli_keeps_lock_through_constructor_work_and_close(root, monkeypatch, capsys):
    events = []
    def locked_event(name):
        assert writer_lock.acquire_run_lock() is None
        events.append(name)
    def construct(_):
        locked_event('construct')
        return SimpleNamespace(close=lambda: locked_event('close'))
    def revive(_):
        locked_event('revive')
        return {'revived': 0, 'skipped_cap': 0}
    monkeypatch.setattr(extract_llm, 'Ledger', construct)
    monkeypatch.setattr(extract_llm_resident, 'Ledger', extract_llm.Ledger)
    monkeypatch.setattr(extract_llm, 'revive_failed', revive)
    monkeypatch.setattr(extract_llm_resident, 'revive_failed', extract_llm.revive_failed)
    monkeypatch.setattr(extract_llm.sys, 'argv', ['extract_llm', '--revive-failed'])
    assert extract_llm.main() == 0
    assert json.loads(capsys.readouterr().out)['revived'] == 0
    assert events == ['construct', 'revive', 'close']
    fd = writer_lock.acquire_run_lock()
    assert fd is not None
    os.close(fd)


@pytest.mark.parametrize('phase', ['selected', 'updating'])
def test_revive_rechecks_restore_before_committing(root, monkeypatch, phase):
    from contextlib import closing
    from common_testkit import make_ledger
    from extract_testkit import _message
    from inference_version import CURRENT_VERSION, VERSION_KEY

    with closing(make_ledger(root)) as ledger:
        ledger.save_messages([_message(mid=1, project_id=1, body='完全合成の記録')])
        ledger.artifact_add(extract_llm.KIND, '{}', project_id=1, message_id=1,
                            meta={VERSION_KEY: CURRENT_VERSION, 'extract_version': extract_llm.EXTRACT_VERSION,
                                  'error': 1, 'attempts': 5, 'auto_retry': 0})
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET created_at=0 WHERE kind=?', (extract_llm.KIND,))
        before = ledger.db.execute('SELECT meta FROM artifacts WHERE kind=?', (extract_llm.KIND,)).fetchone()[0]
        held = []
        prefix = 'SELECT COUNT(*) FROM artifacts' if phase == 'selected' else 'UPDATE artifacts SET meta='
        def interrupt(sql):
            if sql.startswith(prefix) and not held:
                held.append(True)
                (root / 'restore_pending.json').write_text(json.dumps(
                    {'phase': 'awaiting_consent', 'by': 'mcs_update'}))
        ledger.db.set_trace_callback(interrupt)
        try:
            result = extract_llm.revive_failed(ledger, now=extract_llm.REVIVE_COOLDOWN_S + 1)
        finally:
            ledger.db.set_trace_callback(None)
        assert held == [True] and result == {'revived': 0, 'skipped_cap': 0}
        assert ledger.db.execute('SELECT meta FROM artifacts WHERE kind=?', (extract_llm.KIND,)).fetchone()[0] == before
        assert not ledger.db.in_transaction
