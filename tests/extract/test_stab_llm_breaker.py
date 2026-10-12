"""H3 / slow-LLM stability: a refused or 503 endpoint feeds the shared
breaker and the health marker without charging attempts, and a server
that answers /v1/models but times out every call stops charging
attempts once the breaker has tripped. Synthetic fixtures, temp DB and
transport stubs only — no LLM is contacted."""
import json
import time

import pytest

import extract_llm
import local_llm
import mcs_util
from extract_testkit import _ledger, _message


@pytest.fixture(autouse=True)
def _plain(monkeypatch):
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")


def _transport(monkeypatch, kind=None, status=None):
    calls = []

    def chat(prompt, error_out=None, **kw):
        calls.append(prompt)
        if status is not None:
            return {"text": None, "finish_reason": None, "usage": None,
                    "status": status}
        if error_out is not None:
            error_out["kind"] = kind
        return None
    monkeypatch.setattr(local_llm, "chat", chat)
    return calls


def _attempt_rows(db):
    return db.db.execute(
        "SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm'").fetchone()[0]


@pytest.mark.parametrize("kind, status", [("unreachable", None), (None, 503)])
def test_refused_or_loading_endpoint_feeds_breaker_without_attempts(
        tmp_path, monkeypatch, kind, status):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=i, body=f"体温3{i}度") for i in (1, 2)])
        calls = _transport(monkeypatch, kind, status)
        for _ in range(mcs_util._CIRCUIT_TRIP):
            res = extract_llm.run_pending(db, limit=10, budget_s=900)
            assert res["done"] == 0 and res["deferred"] >= 1
        assert calls
        assert _attempt_rows(db) == 0                 # nothing charged
        assert mcs_util.circuit_open_s(db) > 0        # breaker tripped
        health = mcs_util.llm_health(db)
        assert health["down_since"] is not None
        assert mcs_util.llm_down_for_s(db) >= 0
        calls.clear()
        res = extract_llm.run_pending(db, limit=10, budget_s=900)
        assert calls == [] and res["circuit_open_s"]  # lane closed
    finally:
        db.close()


def test_breaker_counts_once_per_run(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=i, body=f"体温3{i}度") for i in range(1, 6)])
        _transport(monkeypatch, "unreachable")
        extract_llm.run_pending(db, limit=10, budget_s=900)
        state = json.loads(mcs_util.circuit_state_path(db).read_text())
        assert state["failures"] == 1
    finally:
        db.close()


def test_slow_endpoint_stops_charging_attempts_after_trip(tmp_path, monkeypatch):
    """/v1/models answers, every completion times out: the first runs still
    charge (a single slow message must reach its ceiling), but once the
    breaker has tripped the rows stay pending at their attempt count."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=1, body="体温38度")])
        monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)
        _transport(monkeypatch, "transport")

        def attempts():
            row = db.db.execute(
                "SELECT meta FROM artifacts WHERE kind='extract_llm'").fetchone()
            return json.loads(row["meta"])["attempts"] if row else 0

        def due_now():
            with db.db:
                db.db.execute(
                    "UPDATE artifacts SET meta=json_set(meta,'$.next_try',0)")
            mcs_util.circuit_state_path(db).write_text(json.dumps({
                **json.loads(mcs_util.circuit_state_path(db).read_text()),
                "open_until": 0}))

        extract_llm.run_pending(db, limit=10, budget_s=900)
        assert attempts() == 1
        for _ in range(mcs_util._CIRCUIT_TRIP - 1):
            due_now()
            res = extract_llm.run_pending(db, limit=10, budget_s=900)
        # the run that tripped the breaker already stops charging
        assert mcs_util.circuit_streak(db) >= 1
        assert res["slow_deferred"] == 1
        charged = attempts()
        assert charged < mcs_util._CIRCUIT_TRIP
        for _ in range(4):
            due_now()
            extract_llm.run_pending(db, limit=10, budget_s=900)
        assert attempts() == charged < 5               # never permanent
        assert mcs_util.llm_down_since(db) is not None
    finally:
        db.close()


def test_answered_call_resets_breaker_and_marks_success(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=1, body="体温38度")])
        mcs_util.circuit_failure(db)
        assert mcs_util.llm_down_since(db) is not None
        monkeypatch.setattr(extract_llm, "llm_extract",
                            lambda body, **_: {"summary": "ok"})
        res = extract_llm.run_pending(db, limit=10, budget_s=900)
        assert res["done"] == 1
        assert not mcs_util.circuit_state_path(db).exists()
        health = mcs_util.llm_health(db)
        assert health["down_since"] is None
        assert health["last_success"] == pytest.approx(time.time(), abs=60)
    finally:
        db.close()


def test_answered_unparseable_call_clears_outage(tmp_path, monkeypatch):
    """A 200 whose text fails to parse charges the message but proves the
    endpoint alive — the outage marker must not outlive the recovery."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=1, body="体温38度")])
        mcs_util.circuit_failure(db)
        assert mcs_util.llm_down_since(db) is not None
        monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)
        monkeypatch.setattr(local_llm, "chat", lambda prompt, **kw: {
            "text": "not-json", "finish_reason": "stop", "usage": None,
            "status": 200})
        res = extract_llm.run_pending(db, limit=1, budget_s=900)
        assert res["failed"] == 1 and res["done"] == 0
        assert not mcs_util.circuit_state_path(db).exists()
        health = mcs_util.llm_health(db)
        assert health["down_since"] is None
        assert health["last_success"] == pytest.approx(time.time(), abs=60)
    finally:
        db.close()


def test_answer_after_same_run_outage_clears_marker(tmp_path, monkeypatch):
    """A 503 then a 200 that fails to parse in one run: the answer proves
    recovery, so the outage marker clears and the failure is charged."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=i, body=f"体温3{i}度") for i in (1, 2)])
        monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)
        replies = iter([{"text": None, "finish_reason": None, "usage": None,
                         "status": 503},
                        {"text": "not-json", "finish_reason": "stop",
                         "usage": None, "status": 200}])
        monkeypatch.setattr(local_llm, "chat", lambda prompt, **kw: next(replies))
        res = extract_llm.run_pending(db, limit=2, budget_s=900, workers=1)
        assert res["deferred"] == 1 and res["failed"] == 1
        health = mcs_util.llm_health(db)
        assert health["down_since"] is None
        assert health["last_success"] == pytest.approx(time.time(), abs=60)
        assert _attempt_rows(db) == 1
    finally:
        db.close()
