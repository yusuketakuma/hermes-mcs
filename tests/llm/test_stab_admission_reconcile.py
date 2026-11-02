"""Unproven completions stay occupying until confirmed retirement or a fence.

Synthetic server-queued requests can coexist with idle slots."""
import json

import pytest

import llm_admission as adm
import local_llm


@pytest.fixture(autouse=True)
def live_owner(monkeypatch):
    monkeypatch.setattr(adm, "_proc_started", lambda pid: "proc:1")
    monkeypatch.setattr(adm, "_pid_alive", lambda pid: True)


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


def test_reconcile_keeps_age_and_unverified_evidence_read_only(broker):
    fresh = _unknown(broker, 10)
    aged = _unknown(broker, adm.UNKNOWN_HOLD_S + 5)
    probes = []

    before = broker.db.total_changes
    for proof in (None, "", "slots_idle_x3", "backend-empty"):
        def evidence():
            probes.append(1)
            return proof
        assert broker.reconcile_unknown(evidence) == {"reconciled": 0, "held": 1}
    assert broker.db.total_changes == before
    assert broker._permit(aged)["state"] == "unknown"
    assert broker._permit(fresh)["state"] == "unknown"
    assert broker.acquire("mcs.extract", "BACKLOG")["reason"] == "class_full"
    assert probes == []


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


@pytest.mark.parametrize("state", ["unknown", "cancel_pending"])
def test_idle_slots_do_not_retire_a_server_queued_request(broker, monkeypatch, state):
    permit = _unknown(broker, adm.UNKNOWN_HOLD_S + 5)
    if state == "cancel_pending":
        broker.cancel(permit)
    queued = [permit]
    idle = [{"id": 0, "is_processing": False}, {"id": 1, "is_processing": False}]
    calls = _slots(monkeypatch, [idle, idle, idle])

    def evidence():
        assert queued == [permit]
        return local_llm.backend_idle_evidence()

    result = broker.reconcile_unknown(evidence)
    assert result == {"reconciled": 0, "held": 1}, {
        "result": result, "idle_samples": len(calls), "server_queued": len(queued)}
    assert broker._permit(permit)["state"] == state and queued == [permit]
    rt = broker.acquire("hermes.interactive", "RT")
    assert not rt["admitted"] and rt["reason"] == "waiting"
    assert not broker.overlap()


def test_idle_observations_cannot_supply_retirement_evidence(monkeypatch):
    idle = [{"id": 0, "is_processing": False}, {"id": 1, "is_processing": False}]
    calls = _slots(monkeypatch, [idle, idle, idle])
    assert local_llm.backend_idle_evidence() is None
    assert calls == []


def test_admitted_chat_holds_queued_work_until_confirmed_retirement(tmp_path, monkeypatch):
    path = str(tmp_path / "adm2.db")
    monkeypatch.setenv("MCS_LLM_ADMISSION", path)
    broker = local_llm._broker(path)
    broker.open_epoch(lambda: True)
    queued = [_unknown(broker, adm.UNKNOWN_HOLD_S + 5) for _ in range(broker.slots)]
    idle = [{"id": 0, "is_processing": False}]
    probes = _slots(monkeypatch, [idle])
    sends = []

    def chat(prompt, **kw):
        assert not queued
        sends.append(1)
        return {"text": "{}", "finish_reason": "stop", "usage": None, "status": 200}

    monkeypatch.setattr(local_llm, "chat", chat)
    monkeypatch.setattr(broker, "reconcile_unknown",
                        lambda *_: pytest.fail("admission denial must not run read-only reconciliation"))
    for _ in range(3):
        out = local_llm.admitted_chat("mcs.extract", "synthetic prompt")
        assert out["admission"] == "class_full" and probes == [] and sends == []
    assert local_llm.admission_status(path)["unresolved"] == broker.slots
    retired = tuple(queued)
    queued.clear()
    for permit in retired:
        assert broker.terminal(permit, "confirmed_retired", proof="synthetic-backend-ack")["terminal"]
    out = local_llm.admitted_chat("mcs.extract", "synthetic prompt")
    assert out["status"] == 200 and "admission" not in out
    assert sends == [1] and probes == []
    assert local_llm.admission_status(path)["unresolved"] == 0
    broker.close()
