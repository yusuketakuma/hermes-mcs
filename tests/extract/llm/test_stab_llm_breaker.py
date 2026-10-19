"""H3 / slow-LLM stability: a refused or 503 endpoint feeds the shared
breaker and the health marker without charging attempts, and a server
that answers /v1/models but times out every call stops charging
attempts once the breaker has tripped. Synthetic fixtures, temp DB and
transport stubs only — no LLM is contacted."""
import llm_availability
import json
import time

import pytest

import extract_llm
import local_llm
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


@pytest.mark.parametrize("batch_k,budget_s", [(0, 90), (2, 400)])
def test_sent_timeout_at_deadline_charges_attempts_without_outage(
        tmp_path, monkeypatch, batch_k, budget_s):
    db = _ledger(tmp_path)
    now = [1000.0]
    calls = []
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: pytest.fail(
        "an expired caller budget must not probe endpoint health"))

    def timeout(prompt, error_out, deadline, **kw):
        calls.append(prompt)
        error_out["kind"] = "transport"
        now[0] = deadline + 1
        return None

    monkeypatch.setattr(local_llm, "chat", timeout)
    try:
        db.save_messages([_message(mid=i, body=f"体温3{i}度") for i in (1, 2)])
        out = extract_llm.run_pending(db, limit=2, budget_s=budget_s, batch_k=batch_k)
        assert len(calls) == 1
        expected = 2 if batch_k else 1
        assert out["failed"] == expected
        rows = db.db.execute("SELECT meta FROM artifacts WHERE kind='extract_llm'").fetchall()
        assert len(rows) == expected
        assert all(json.loads(row["meta"])["attempts"] == 1 for row in rows)
        assert not llm_availability.circuit_state_path(db.db).exists()
        assert llm_availability.llm_down_since(db.db) is None
    finally:
        db.close()


@pytest.mark.parametrize("kind, status", [("unreachable", None), (None, 503)])
def test_refused_or_loading_endpoint_feeds_breaker_without_attempts(
        tmp_path, monkeypatch, kind, status):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=i, body=f"体温3{i}度") for i in (1, 2)])
        calls = _transport(monkeypatch, kind, status)
        for _ in range(llm_availability._CIRCUIT_TRIP):
            res = extract_llm.run_pending(db, limit=10, budget_s=900)
            assert res["done"] == 0 and res["deferred"] >= 1
        assert calls
        assert _attempt_rows(db) == 0                 # nothing charged
        assert llm_availability.circuit_open_s(db.db) > 0        # breaker tripped
        health = llm_availability.llm_health(db.db)
        assert health["down_since"] is not None
        assert llm_availability.llm_down_for_s(db.db) >= 0
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
        state = json.loads(llm_availability.circuit_state_path(db.db).read_text())
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
            llm_availability.circuit_state_path(db.db).write_text(json.dumps({
                **json.loads(llm_availability.circuit_state_path(db.db).read_text()),
                "open_until": 0}))

        extract_llm.run_pending(db, limit=10, budget_s=900)
        assert attempts() == 1
        for _ in range(llm_availability._CIRCUIT_TRIP - 1):
            due_now()
            res = extract_llm.run_pending(db, limit=10, budget_s=900)
        # the run that tripped the breaker already stops charging
        assert llm_availability.circuit_streak(db.db) >= 1
        assert res["slow_deferred"] == 1
        charged = attempts()
        assert charged < llm_availability._CIRCUIT_TRIP
        for _ in range(4):
            due_now()
            extract_llm.run_pending(db, limit=10, budget_s=900)
        assert attempts() == charged < 5               # never permanent
        assert llm_availability.llm_down_since(db.db) is not None
    finally:
        db.close()


def test_answered_call_resets_breaker_and_marks_success(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=1, body="体温38度")])
        llm_availability.circuit_failure(db.db)
        assert llm_availability.llm_down_since(db.db) is not None
        monkeypatch.setattr(extract_llm, "llm_extract",
                            lambda body, **_: {"summary": "ok"})
        res = extract_llm.run_pending(db, limit=10, budget_s=900)
        assert res["done"] == 1
        assert not llm_availability.circuit_state_path(db.db).exists()
        health = llm_availability.llm_health(db.db)
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
        llm_availability.circuit_failure(db.db)
        assert llm_availability.llm_down_since(db.db) is not None
        monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)
        monkeypatch.setattr(local_llm, "chat", lambda prompt, **kw: {
            "text": "not-json", "finish_reason": "stop", "usage": None,
            "status": 200})
        res = extract_llm.run_pending(db, limit=1, budget_s=900)
        assert res["failed"] == 1 and res["done"] == 0
        assert not llm_availability.circuit_state_path(db.db).exists()
        health = llm_availability.llm_health(db.db)
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
        health = llm_availability.llm_health(db.db)
        assert health["down_since"] is None
        assert health["last_success"] == pytest.approx(time.time(), abs=60)
        assert _attempt_rows(db) == 1
    finally:
        db.close()


def test_outage_after_same_run_recovery_is_recorded(tmp_path, monkeypatch):
    """503, a 200 that fails to parse, then 503 again in one run: the
    answer ends the first outage and the last 503 starts a new one, so the
    marker reflects the endpoint's latest state."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=i, body=f"体温3{i}度") for i in (1, 2, 3)])
        monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)
        down = {"text": None, "finish_reason": None, "usage": None, "status": 503}
        replies = iter([down, {"text": "not-json", "finish_reason": "stop",
                               "usage": None, "status": 200}, down])
        monkeypatch.setattr(local_llm, "chat", lambda prompt, **kw: next(replies))
        res = extract_llm.run_pending(db, limit=3, budget_s=900, workers=1)
        assert res["deferred"] == 2 and res["failed"] == 1
        assert llm_availability.llm_down_since(db.db) is not None
    finally:
        db.close()


def test_unusable_200_body_clears_outage(tmp_path, monkeypatch):
    """chat returns None with kind=protocol for a 200 whose body cannot be
    used: the endpoint still answered, so the outage marker clears."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=1, body="体温38度")])
        llm_availability.circuit_failure(db.db)
        monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)
        _transport(monkeypatch, "protocol")
        res = extract_llm.run_pending(db, limit=1, budget_s=900)
        assert res["failed"] == 1
        assert llm_availability.llm_down_since(db.db) is None
    finally:
        db.close()


def test_answer_then_deferral_still_clears_outage(tmp_path, monkeypatch):
    """An answered call whose message is then deferred (no time left to
    repair its output) still records the recovery."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=1, body="体温38度")])
        llm_availability.circuit_failure(db.db)

        def answered_then_deferred(body, **kw):
            extract_llm._CALL_NOTES.transport = "answered"
            return extract_llm._DEFERRED
        monkeypatch.setattr(extract_llm, "llm_extract", answered_then_deferred)
        res = extract_llm.run_pending(db, limit=1, budget_s=900)
        assert res["deferred"] == 1 and _attempt_rows(db) == 0
        assert llm_availability.llm_down_since(db.db) is None
    finally:
        db.close()


def _noted(kind, at, result):
    """An llm_extract stand-in that records its call's endpoint note."""
    def call(body, **kw):
        extract_llm._CALL_NOTES.transport = kind
        extract_llm._CALL_NOTES.transport_at = at
        return result
    return call


def test_older_answer_does_not_clear_a_newer_outage(tmp_path, monkeypatch):
    """Futures finish out of order: a 2xx that completed before a 503 but is
    handled after it must not erase the newer outage."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=i, body=f"体温3{i}度") for i in (1, 2)])
        monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)
        calls = iter([("unavailable", 2.0, extract_llm._DEFERRED),
                      ("answered", 1.0, None)])

        def call(body, **kw):
            kind, at, result = next(calls)
            return _noted(kind, at, result)(body)
        monkeypatch.setattr(extract_llm, "llm_extract", call)
        extract_llm.run_pending(db, limit=2, budget_s=900, workers=1)
        assert llm_availability.llm_down_since(db.db) is not None
    finally:
        db.close()


def test_answered_batch_then_deferred_residue_clears_outage(tmp_path, monkeypatch):
    """A batch call answered with an unusable envelope and its rows then
    deferred: the batch's answer still records the recovery."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=i, body=f"体温3{i}度") for i in (1, 2)])
        llm_availability.circuit_failure(db.db)

        def batch(tups, *a, **kw):
            extract_llm._CALL_NOTES.transport = "answered"
            extract_llm._CALL_NOTES.transport_at = time.monotonic()
            return "failed", {}, {"batch": len(tups)}
        monkeypatch.setattr(extract_llm, "_run_batch_call", batch)
        monkeypatch.setattr(extract_llm, "llm_extract",
                            _noted(None, None, extract_llm._DEFERRED))
        res = extract_llm.run_pending(db, limit=2, budget_s=900, batch_k=2)
        assert res["deferred"] == 2
        assert llm_availability.llm_down_since(db.db) is None
    finally:
        db.close()


def test_failed_repair_keeps_the_outage(tmp_path, monkeypatch):
    """A usable first answer whose repair call then got a 503: the row is
    saved, but the endpoint's latest observation is an outage."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=1, body="体温38度")])
        monkeypatch.setattr(extract_llm, "llm_extract",
                            _noted("unavailable", time.monotonic(), {"summary": "ok"}))
        res = extract_llm.run_pending(db, limit=1, budget_s=900)
        assert res["done"] == 1
        assert llm_availability.llm_down_since(db.db) is not None
    finally:
        db.close()


def test_turn_wait_expiry_defers_without_breaker_or_attempts(tmp_path, monkeypatch):
    """A backlog lane whose deadline passed while the other lane held the
    GPU turn sent nothing: the rows stay pending, no attempt is charged
    and the breaker/health marker see no outage."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=i, body=f"体温3{i}度") for i in (1, 2)])
        calls = _transport(monkeypatch, "busy")
        for _ in range(llm_availability._CIRCUIT_TRIP):
            res = extract_llm.run_pending(db, limit=10, budget_s=900)
            assert res["done"] == 0 and res["deferred"] >= 1
        assert calls
        assert _attempt_rows(db) == 0
        assert not llm_availability.circuit_state_path(db.db).exists()
        assert llm_availability.llm_down_since(db.db) is None
    finally:
        db.close()
