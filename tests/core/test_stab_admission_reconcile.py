"""Completion-unproven permits of a live (resident) owner are released
only after the hold AND backend evidence; occupancy is visible for
alerting. Synthetic broker store and stubbed /slots only."""
import json

import pytest

import llm_admission as adm
import local_llm


@pytest.fixture
def broker(tmp_path):
    b = adm.Broker(str(tmp_path / "adm.db"), slots=2)
    b.open_epoch(lambda: True)
    yield b
    b.close()


def _unknown(broker, age_s):
    acq = broker.acquire("mcs.extract", "BACKLOG")
    pid = acq["permit_id"]
    assert broker.sent(pid)["sent"]
    broker.mark_unknown(pid, "transport")
    with broker.db:
        broker.db.execute(
            "UPDATE permits SET sent_at=sent_at-?, admitted_at=admitted_at-?,"
            " created_at=created_at-? WHERE permit_id=?",
            (age_s, age_s, age_s, pid))
    return pid


def test_unknown_permits_fill_width_and_show_in_status(broker):
    _unknown(broker, 10)
    _unknown(broker, 10)
    assert broker.acquire("mcs.extract", "BACKLOG")["reason"] == "class_full"
    status = broker.status()
    assert status["unresolved"] == 2 and status["slots"] == 2
    assert status["oldest_unresolved_age_s"] >= 10


def test_reconcile_needs_age_and_evidence(broker):
    fresh = _unknown(broker, 10)
    aged = _unknown(broker, adm.UNKNOWN_HOLD_S + 5)
    probes = []

    def no_proof():
        probes.append(1)
        return None
    assert broker.reconcile_unknown(no_proof) == {"reconciled": 0, "held": 1}
    assert broker.reconcile_unknown(lambda: "") ["reconciled"] == 0
    assert broker._permit(aged)["state"] == "unknown"
    assert broker.reconcile_unknown(lambda: "slots_idle_x3") == {"reconciled": 1}
    row = broker._permit(aged)
    assert (row["state"], row["outcome"], row["proof"]) == \
        ("terminal", "reconciled", "slots_idle_x3")
    assert broker._permit(fresh)["state"] == "unknown"     # still held
    assert broker.acquire("mcs.extract", "BACKLOG")["admitted"]
    assert probes == [1]


def test_reconcile_without_candidates_never_probes(broker):
    _unknown(broker, 10)
    assert broker.reconcile_unknown(
        lambda: pytest.fail("probe without aged candidates")) == {"reconciled": 0}


def _slots(monkeypatch, payloads):
    calls = []

    def request(url, method, body, timeout, deadline=None):
        calls.append(url)
        return 200, {}, json.dumps(payloads[min(len(calls), len(payloads)) - 1]).encode()
    monkeypatch.setattr(local_llm, "bounded_request", request)
    monkeypatch.setattr(local_llm.time, "sleep", lambda s: None)
    return calls


def test_idle_evidence_requires_every_sample_idle(monkeypatch):
    idle = [{"id": 0, "is_processing": False}, {"id": 1, "is_processing": False}]
    busy = [{"id": 0, "is_processing": True}, {"id": 1, "is_processing": False}]
    calls = _slots(monkeypatch, [idle, idle, idle])
    assert local_llm.backend_idle_evidence() == "slots_idle_x3"
    assert len(calls) == 3 and all(c.endswith("/slots") for c in calls)
    _slots(monkeypatch, [idle, busy, idle])
    assert local_llm.backend_idle_evidence() is None
    _slots(monkeypatch, [[]])
    assert local_llm.backend_idle_evidence() is None


def test_admitted_chat_reconciles_aged_unknowns_once(tmp_path, monkeypatch):
    path = str(tmp_path / "adm2.db")
    monkeypatch.setenv("MCS_LLM_ADMISSION", path)
    monkeypatch.setattr(local_llm, "_RECONCILED_AT", {})
    broker = local_llm._broker(path)
    broker.open_epoch(lambda: True)
    for _ in range(broker.slots):
        _unknown(broker, adm.UNKNOWN_HOLD_S + 5)
    idle = [{"id": 0, "is_processing": False}]
    probes = _slots(monkeypatch, [idle])
    monkeypatch.setattr(local_llm, "chat", lambda prompt, **kw: {
        "text": "{}", "finish_reason": "stop", "usage": None, "status": 200})
    out = local_llm.admitted_chat("mcs.extract", "synthetic prompt")
    assert out["status"] == 200 and "admission" not in out
    assert len(probes) == 3
    status = local_llm.admission_status(path)
    assert status["unresolved"] == 0
    # a second aged permit inside the interval is not probed again
    _unknown(broker, adm.UNKNOWN_HOLD_S + 5)
    for _ in range(broker.slots - 1):
        _unknown(broker, adm.UNKNOWN_HOLD_S + 5)
    out = local_llm.admitted_chat("mcs.extract", "synthetic prompt")
    assert out["admission"] == "class_full" and len(probes) == 3
    broker.close()
