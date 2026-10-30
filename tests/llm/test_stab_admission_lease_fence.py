"""Accepted server queues outlive an owner; only unsent leases are reclaimable."""
import time

import pytest

import llm_admission as adm


@pytest.fixture
def broker(tmp_path, monkeypatch):
    monkeypatch.setattr(adm, "_proc_started", lambda pid: "proc:1000")
    monkeypatch.setattr(adm, "_pid_alive", lambda pid: True)
    store = adm.Broker(str(tmp_path / "admission.db"))
    assert store.open_epoch(lambda: True)
    yield store
    store.close()


def _expired_owner(broker, permit, monkeypatch, owner):
    with broker.db:
        broker.db.execute(
            "UPDATE permits SET owner_pid=9000001, owner_started='proc:1000',"
            " created_at=created_at-?, admitted_at=admitted_at-?, sent_at=sent_at-?"
            " WHERE permit_id=?", (adm.LEASE_S + 1,) * 3 + (permit,))
    monkeypatch.setattr(adm, "_pid_alive", lambda pid: owner != "dead" or pid != 9000001)
    monkeypatch.setattr(adm, "_proc_started",
                        lambda pid: "proc:2000" if pid == 9000001 else "proc:1000")


def _queued_request(broker, state):
    permit = broker.acquire("gbrain.query", "RT")["permit_id"]
    sent = broker.sent(permit)
    assert sent["sent"] and broker.token_valid(sent["token"], "RT")["ok"]
    queued = {permit}  # The server already accepted it; fencing cannot retract that acceptance.
    if state == "unknown":
        broker.mark_unknown(permit, "synthetic transport loss")
    elif state == "cancel_pending":
        broker.cancel(permit)
    return permit, queued


@pytest.mark.parametrize("state", ["sent", "unknown", "cancel_pending"])
@pytest.mark.parametrize("owner", ["dead", "replaced"])
def test_owner_loss_does_not_retire_an_accepted_server_queue(broker, monkeypatch, state, owner):
    permit, queued = _queued_request(broker, state)
    _expired_owner(broker, permit, monkeypatch, owner)
    result = broker.acquire("mcs.extract", "BACKLOG")
    assert not result["admitted"] and result["reason"] == "rt_pending", {
        "admission": result, "server_queued": len(queued)}
    assert broker._permit(permit)["state"] == state and queued == {permit}
    assert broker.status()["occupying"] == {"RT": 1, "BACKLOG": 0}
    queued.clear()
    assert broker.terminal(permit, "confirmed_retired", proof="synthetic-server-ack")["terminal"]
    assert broker.acquire("mcs.extract", "BACKLOG")["admitted"]


@pytest.mark.parametrize("state", ["reserved", "admitted", "waiting"])
@pytest.mark.parametrize("owner", ["dead", "replaced"])
def test_only_proven_unsent_permits_are_reclaimed(broker, monkeypatch, state, owner):
    if state == "waiting":
        background = broker.acquire("mcs.extract", "BACKLOG")["permit_id"]
        pending = broker.acquire("gbrain.query", "RT")
        assert pending["reason"] == "waiting"
        permit = pending["permit_id"]
        broker.terminal(background, "not_sent", proof="synthetic-never-sent")
    else:
        permit = broker.acquire("gbrain.query", "RT")["permit_id"]
        with broker.db:
            broker.db.execute("UPDATE permits SET state=? WHERE permit_id=?", (state, permit))
    row = broker._permit(permit)
    assert row["token"] is None and row["sent_at"] is None
    _expired_owner(broker, permit, monkeypatch, owner)
    assert broker.acquire("mcs.extract", "BACKLOG")["admitted"]
    row = broker._permit(permit)
    assert (row["state"], row["outcome"]) == ("fenced", "owner_dead")
    assert row["proof"] == "owner_dead_unsent"
    assert row["token"] is None and row["sent_at"] is None
    assert broker.status()["rt_waiting"] == 0


@pytest.mark.parametrize("marker", ["token", "sent_at"])
@pytest.mark.parametrize("owner", ["dead", "replaced"])
def test_contradictory_send_marker_is_never_reclaimed(broker, monkeypatch, marker, owner):
    permit = broker.acquire("gbrain.query", "RT")["permit_id"]
    value = "synthetic-dispatch-token" if marker == "token" else time.time()
    with broker.db:
        broker.db.execute(f"UPDATE permits SET {marker}=? WHERE permit_id=?", (value, permit))
    _expired_owner(broker, permit, monkeypatch, owner)
    assert broker.acquire("mcs.extract", "BACKLOG")["reason"] == "rt_pending"
    assert broker._permit(permit)["state"] == "admitted"


@pytest.mark.parametrize("state", ["sent", "unknown", "cancel_pending"])
def test_missing_legacy_send_markers_do_not_prove_unsent(broker, monkeypatch, state):
    permit, queued = _queued_request(broker, state)
    with broker.db:
        broker.db.execute("UPDATE permits SET token=NULL,sent_at=NULL WHERE permit_id=?", (permit,))
    _expired_owner(broker, permit, monkeypatch, "dead")
    assert broker.acquire("mcs.extract", "BACKLOG")["reason"] == "rt_pending"
    assert broker._permit(permit)["state"] == state and queued == {permit}


def test_epoch_recovery_stays_closed_until_its_caller_confirms_empty(broker):
    permit, queued = _queued_request(broker, "unknown")
    epoch, path = broker.epoch(), broker.path
    broker.close()
    recovered = adm.Broker(path)
    try:
        assert recovered.epoch() == epoch + 1 and not recovered.is_open()
        assert recovered._permit(permit)["state"] == "fenced" and queued == {permit}
        assert recovered.acquire("mcs.extract", "BACKLOG")["reason"] == "epoch_closed"
        assert not recovered.open_epoch(lambda: not queued)
        assert recovered.acquire("mcs.extract", "BACKLOG")["reason"] == "epoch_closed"
        queued.clear()
        assert recovered.open_epoch(lambda: not queued)
        assert recovered.acquire("mcs.extract", "BACKLOG")["admitted"]
    finally:
        recovered.close()


@pytest.mark.parametrize("proof,clear_token,clear_sent_at", [
    (None, False, False),
    (None, True, True),
    ("legacy-idle-evidence", True, True),
    ("owner_dead_unsent", False, True),
    ("owner_dead_unsent", True, False),
], ids=["old-dispatched", "old-missing-markers", "arbitrary-proof",
        "proof-with-token", "proof-with-sent-at"])
def test_old_owner_fence_cannot_reopen_with_an_accepted_queue(
        broker, proof, clear_token, clear_sent_at):
    permit, queued = _queued_request(broker, "unknown")
    row = broker._permit(permit)
    with broker.db:
        broker.db.execute(
            "UPDATE permits SET state='fenced',outcome='owner_dead',proof=?,"
            " token=?,sent_at=?,terminal_at=? WHERE permit_id=?",
            (proof, None if clear_token else row["token"],
             None if clear_sent_at else row["sent_at"], time.time(), permit))
    saved = dict(broker._permit(permit))
    epoch, path = broker.epoch(), broker.path
    assert broker.is_open() and broker.status()["occupying"] == {"RT": 0, "BACKLOG": 0}
    broker.close()
    recovered = adm.Broker(path)
    try:
        assert not recovered.is_open(), {"epoch": recovered.epoch(), "server_queued": len(queued)}
        assert recovered.epoch() == epoch + 1 and queued == {permit}
        assert dict(recovered._permit(permit)) == saved
        assert recovered.acquire("mcs.extract", "BACKLOG")["reason"] == "epoch_closed"
        assert not recovered.open_epoch(lambda: not queued)
        assert recovered.acquire("mcs.extract", "BACKLOG")["reason"] == "epoch_closed"
        queued.clear()
        assert recovered.open_epoch(lambda: not queued)
        assert recovered.acquire("mcs.extract", "BACKLOG")["admitted"]
    finally:
        recovered.close()


def test_proven_unsent_owner_fence_preserves_its_proof_on_recovery(broker, monkeypatch):
    permit = broker.acquire("gbrain.query", "RT")["permit_id"]
    _expired_owner(broker, permit, monkeypatch, "dead")
    background = broker.acquire("mcs.extract", "BACKLOG")
    assert background["admitted"]
    saved = dict(broker._permit(permit))
    assert saved["proof"] == "owner_dead_unsent"
    assert saved["token"] is None and saved["sent_at"] is None
    broker.terminal(background["permit_id"], "not_sent", proof="synthetic-never-sent")
    epoch, path = broker.epoch(), broker.path
    broker.close()
    recovered = adm.Broker(path)
    try:
        assert recovered.epoch() == epoch and recovered.is_open()
        assert dict(recovered._permit(permit)) == saved
        assert recovered.acquire("mcs.extract", "BACKLOG")["admitted"]
    finally:
        recovered.close()
