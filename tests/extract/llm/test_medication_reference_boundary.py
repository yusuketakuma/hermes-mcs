"""Zero-result tampering and owner-read failures remain unsent reference changes."""
from copy import deepcopy
import sqlite3
import time
from types import SimpleNamespace

import pytest

import cloud_inference
import extract_llm
import extraction_medications as references
from test_medication_reference_input import configured, snapshot, store  # noqa: F401


def test_removing_every_candidate_is_not_a_verified_empty_scan(store, tmp_path):  # noqa: F811
    body = "キラナを服用。"
    cfg, value = snapshot(store, tmp_path, body)
    emptied = deepcopy(value)
    emptied["parts"][0]["hint"]["matches"] = []
    assert references.valid(emptied, body)
    assert not references.has_private(emptied) and references.uses_dictionary(emptied)
    assert not references.current(store.db, cfg, emptied, body=body)


def test_genuine_empty_scan_still_checks_its_dictionary_generation(store, tmp_path):  # noqa: F811
    body = "本人から連絡。"
    cfg = configured(store, tmp_path)
    value = references.build(store.db, cfg, body, private=True, deadline=time.monotonic() + 5)
    assert references.valid(value, body) and references.uses_dictionary(value)
    assert not references.has_private(value)
    assert references.current(store.db, cfg, value, body=body)
    assert not references.current(store.db, {}, value, body=body)


def test_owner_database_failure_is_false_before_any_transport(monkeypatch):
    def broken(*args):
        raise sqlite3.OperationalError("synthetic owner read failure")
    monkeypatch.setattr(extract_llm, "_owns_current_source", broken)
    assert not extract_llm._extraction_dispatch_current(SimpleNamespace(db=object()), {}, 1,
                                                        time.monotonic() + 5)


def test_owner_database_failure_at_http_boundary_is_proven_unsent(monkeypatch):
    def broken(*args):
        raise sqlite3.OperationalError("synthetic owner read failure")
    monkeypatch.setattr(extract_llm, "_owns_current_source", broken)
    monkeypatch.setattr(extract_llm._CALL_NOTES, "medication_source_changed", False, raising=False)
    monkeypatch.setattr(extract_llm._CALL_NOTES, "medication_guard", lambda:
        extract_llm._extraction_dispatch_current(SimpleNamespace(db=object()), {}, 1,
                                                  time.monotonic() + 5), raising=False)
    monkeypatch.setattr(extract_llm.local_llm, "bounded_request", lambda *a: pytest.fail("sent after owner read error"))
    with pytest.raises(extract_llm.bounded_http.NotSentTimeout):
        extract_llm._opener_request("http://localhost", "POST", {}, 1)
    assert extract_llm._CALL_NOTES.medication_source_changed is True


def test_lost_owner_at_http_boundary_does_not_enter_the_http_worker(monkeypatch):
    def stopped():
        raise cloud_inference.CloudFallback("cloud_owner_stopped")
    monkeypatch.setattr(extract_llm._CALL_NOTES, "medication_source_changed", False, raising=False)
    monkeypatch.setattr(extract_llm._CALL_NOTES, "medication_guard", stopped, raising=False)
    monkeypatch.setattr(extract_llm.local_llm, "bounded_request", lambda *a: pytest.fail("sent without owner"))
    with pytest.raises(extract_llm.bounded_http.NotSentTimeout):
        extract_llm._opener_request("http://localhost", "POST", {}, 1)


def test_expired_dispatch_does_not_read_the_source_database(monkeypatch):
    monkeypatch.setattr(extract_llm, "_owns_current_source", lambda *a: pytest.fail("read after dispatch deadline"))
    assert not extract_llm._extraction_dispatch_current(SimpleNamespace(db=object()), {}, 1,
                                                        time.monotonic() - 1)
