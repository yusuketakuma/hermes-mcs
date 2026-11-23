"""A room the collector recorded as not permitted is closed to original-source reads (temp DB + stubs)."""
import time
from types import SimpleNamespace

import pytest

import collection_stages
import mcs_contract
from ingest_testkit import _ledger, _message, _unread_patient
from patient_read_scope import patient_scope_valid


def _adapter(fail_kind):
    def fetch(*_):
        if fail_kind:
            raise mcs_contract.MCSError(fail_kind, "synthetic", status=403)
        return mcs_contract.MessageBatch(messages=[_message(mid=5, unread=True)], reached=True)
    return SimpleNamespace(
        list_unread=lambda: mcs_contract.UnreadSnapshot(timestamp=123, patients=[_unread_patient(1)]),
        fetch_unread_messages=fetch,
        fetch_unread_replies=lambda *_: mcs_contract.ReplyBatch([], []),
        fetch_latest=lambda pid: {"message_id": 5, "is_self_only": False},
        mark_patient_read=lambda *a, **k: None)


def _collect(db, fail_kind):
    result = {"errors": [], "incomplete": [], "messages": 0, "new_messages": 0, "marked_read": []}
    collection_stages.stage_unread(_adapter(fail_kind), db, result, time.monotonic() + 30,
                                   db.begin_run(None), mark_read=False)


def _state(db):
    return tuple(db.db.execute("SELECT fetch_state,fetch_reason FROM patients WHERE project_id=1").fetchone())


def test_collector_records_forbidden_as_a_reason_and_the_read_scope_closes_the_room(tmp_path):
    db = _ledger(tmp_path)
    try:
        _collect(db, None)
        assert _state(db) == ("complete", "") and patient_scope_valid(db.db, 1)
        # The collector words a 403 as fetch_reason='forbidden' with fetch_state='incomplete'.
        _collect(db, "forbidden")
        assert _state(db) == ("incomplete", "forbidden")
        assert not patient_scope_valid(db.db, 1)
        db.patient_fetch_failed(1, "forbidden")
        assert _state(db) == ("incomplete", "forbidden") and not patient_scope_valid(db.db, 1)
    finally:
        db.close()


@pytest.mark.parametrize("state,reason", [
    ("forbidden", None), ("forbidden", "timeout"),          # a legacy row stays closed
    ("incomplete", "permission_denied"), ("incomplete", "access_revoked"), ("complete", "forbidden")])
def test_every_not_permitted_record_closes_the_room(tmp_path, state, reason):
    db = _ledger(tmp_path)
    try:
        db.ensure_patient(1)
        db.db.execute("UPDATE patients SET fetch_state=?,fetch_reason=? WHERE project_id=1", (state, reason))
        assert not patient_scope_valid(db.db, 1)
        db.db.execute("UPDATE patients SET fetch_state='incomplete',fetch_reason='timeout' WHERE project_id=1")
        assert patient_scope_valid(db.db, 1)        # a plain fetch failure keeps stored sources readable
    finally:
        db.close()
