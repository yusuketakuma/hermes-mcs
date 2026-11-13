"""The quality fence is this patient's inputs, re-verified after foreign commits."""
from contextlib import closing

import pytest

from extract_testkit import _message
from ledger import Ledger
import patient_context_quality_controller as controller
import patient_context_quality_receipts as receipts
import patient_context_refresh_state as state
from test_patient_context_refresh import runtime
from test_patient_summary_quality import quality_owner

__all__ = ['quality_owner', 'runtime']


def _other(led):
    return Ledger(led.db.execute('PRAGMA database_list').fetchone()[2])


def _during_lock_wait(monkeypatch, action):
    """Run ``action`` after the caller's read fence, while it waits for the run lock."""
    original = receipts._acquire
    calls = []

    def acquire(*args, **kwargs):
        calls.append(True)
        action()
        return original(*args, **kwargs)

    monkeypatch.setattr(receipts, '_acquire', acquire)
    return calls


def _step(led, cfg, doc, clock):
    return controller._quality_step(led, 1, cfg, '2026-10-17', doc, clock.mono + 600, lambda: True)


def test_unrelated_commit_while_waiting_for_the_lock_still_claims_and_saves(quality_owner, monkeypatch):
    led, clock, cfg, doc, sends, _ = quality_owner

    def unrelated():
        with closing(_other(led)) as other:
            other.ensure_patient(2)

    calls = _during_lock_wait(monkeypatch, unrelated)
    status, _ = _step(led, cfg, doc, clock)
    # Claim and answer storage both crossed a foreign commit.
    assert len(calls) >= 2
    assert status == 'pending' and len(sends) == 1
    assert led.artifacts(state.QUALITY)


def test_same_patient_source_change_is_refused(quality_owner, monkeypatch):
    led, clock, cfg, doc, sends, _ = quality_owner

    def new_source():
        with closing(_other(led)) as other:
            other.save_messages([_message(90, '生活環境: 合成の追加記録。', 'full', project_id=1)])
            other.db.commit()

    _during_lock_wait(monkeypatch, new_source)
    with pytest.raises(ValueError, match='patient_summary_quality_source_changed'):
        _step(led, cfg, doc, clock)
    assert not sends and not led.artifacts(state.QUALITY)


def test_unexpected_own_write_in_the_writer_transaction_is_refused(quality_owner, monkeypatch):
    led, clock, cfg, doc, sends, _ = quality_owner
    original = receipts._quality_owns
    wrote = []

    def owns_after_own_write(db, *args, **kwargs):
        # Once: the later release transaction is the owner's own and may commit.
        if db.in_transaction and not wrote:
            wrote.append(True)
            db.execute("INSERT OR IGNORE INTO patients(project_id,last_seen,created_at) VALUES(3,0,0)")
        return original(db, *args, **kwargs)

    monkeypatch.setattr(receipts, '_quality_owns', owns_after_own_write)
    try:
        status, _ = _step(led, cfg, doc, clock)
    except Exception:
        status = None
    assert status != 'pending'
    assert not led.artifacts(state.QUALITY)
    assert not led.db.execute('SELECT 1 FROM patients WHERE project_id=3').fetchone()
