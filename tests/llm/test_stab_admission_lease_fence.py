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


def _legacy_owner_fence(broker, permit, *, expired=False):
    retired_at = time.time() - (adm.RETENTION_S + 1 if expired else 0)
    with broker.db:
        broker.db.execute(
            "UPDATE permits SET state='fenced',outcome='owner_dead',"
            "proof=NULL,terminal_at=? WHERE permit_id=?", (retired_at, permit))


def test_shared_join_closes_legacy_queue_before_admission_and_pruning(broker):
    permit, queued = _queued_request(broker, "unknown")
    _legacy_owner_fence(broker, permit, expired=True)
    saved, epoch = dict(broker._permit(permit)), broker.epoch()
    joined = adm.Broker(broker.path)
    try:
        assert joined.epoch() == epoch + 1 and not joined.is_open()
        assert dict(joined._permit(permit)) == saved
        result = joined.acquire("mcs.extract", "BACKLOG")
        assert not result["admitted"], {"admission": result, "server_queued": len(queued)}
        assert result["reason"] == "epoch_closed"
        assert joined.epoch() == broker.epoch() == epoch + 1
        assert not joined.is_open() and queued == {permit}
        assert dict(joined._permit(permit)) == saved
        assert not broker.open_epoch(lambda: not queued)
        assert dict(joined._permit(permit)) == saved
        queued.clear()
        assert broker.open_epoch(lambda: not queued)
        assert joined.acquire("mcs.extract", "BACKLOG")["admitted"]
        # Existing retention can prune after the trusted caller reopens.
        assert joined._permit(permit) is None
    finally:
        joined.close()


@pytest.mark.parametrize("gate", [
    "acquire", "sent", "token_valid", "poll", "open_epoch",
    "terminal", "cancel", "mark_unknown",
])
def test_live_shared_gates_close_a_later_legacy_fence(broker, gate):
    joined = adm.Broker(broker.path)
    try:
        cls = "BACKLOG" if gate == "poll" else "RT"
        route = "mcs.extract" if cls == "BACKLOG" else "gbrain.query"
        permit = broker.acquire(route, cls)["permit_id"]
        assert broker.sent(permit)["sent"]
        queued = {permit}
        candidate = None
        if gate in ("sent", "token_valid", "poll"):
            candidate = broker.acquire("gbrain.query", "RT")["permit_id"]
            if gate == "token_valid":
                assert broker.sent(candidate)["sent"]
        token = broker._permit(candidate or permit)["token"]
        _legacy_owner_fence(broker, permit)
        saved, epoch = dict(broker._permit(permit)), broker.epoch()
        if gate == "acquire":
            assert joined.acquire("mcs.extract", "BACKLOG")["reason"] == "epoch_closed"
        elif gate == "sent":
            assert joined.sent(candidate)["reason"] == "stale_epoch"
        elif gate == "token_valid":
            assert joined.token_valid(token, "RT")["reason"] == "stale_epoch"
        elif gate == "poll":
            assert joined.poll(candidate)["state"] == "fenced"
        elif gate == "open_epoch":
            def verify_empty():
                assert joined.epoch() == epoch + 1 and not joined.is_open()
                return not queued
            assert not joined.open_epoch(verify_empty)
        else:
            result = (joined.terminal(permit, "unproven", proof="synthetic")
                      if gate == "terminal" else getattr(joined, gate)(permit))
            assert result["reason"] == "stale_epoch"
        assert joined.epoch() == broker.epoch() == epoch + 1
        assert not joined.is_open() and not broker.is_open() and queued == {permit}
        assert dict(joined._permit(permit)) == saved
        assert not joined.token_valid(token, cls)["ok"]
        assert joined.acquire("mcs.extract", "BACKLOG")["reason"] == "epoch_closed"
        queued.clear()
        assert joined.open_epoch(lambda: not queued)
        assert broker.acquire("mcs.extract", "BACKLOG")["admitted"]
    finally:
        joined.close()


def test_guard_commit_failure_rolls_back_without_an_early_commit(broker, monkeypatch):
    joined = adm.Broker(broker.path)
    try:
        permit, queued = _queued_request(broker, "sent")
        _legacy_owner_fence(broker, permit)
        saved, epoch = dict(broker._permit(permit)), broker.epoch()

        connection = broker.db

        class FailedCommit:
            def __getattr__(self, name):
                return getattr(connection, name)

            def commit(self):
                assert connection.in_transaction
                assert broker.epoch() == epoch + 1 and not broker.is_open()
                assert joined.epoch() == epoch and joined.is_open()
                raise RuntimeError("synthetic-after-guard")

        monkeypatch.setattr(broker, "db", FailedCommit())
        with pytest.raises(RuntimeError, match="synthetic-after-guard"):
            broker.acquire("mcs.extract", "BACKLOG")
        monkeypatch.setattr(broker, "db", connection)
        assert not broker.db.in_transaction
        assert broker.epoch() == epoch and dict(broker._permit(permit)) == saved
        assert joined.acquire("mcs.extract", "BACKLOG")["reason"] == "epoch_closed"
        assert broker.epoch() == epoch + 1 and not broker.is_open() and queued == {permit}
    finally:
        joined.close()


def test_failed_trusted_assertion_keeps_the_guard_committed(broker):
    joined = adm.Broker(broker.path)
    try:
        permit, queued = _queued_request(broker, "sent")
        candidate = broker.acquire("gbrain.query", "RT")["permit_id"]
        token = broker.sent(candidate)["token"]
        _legacy_owner_fence(broker, permit)
        saved, epoch = dict(broker._permit(permit)), broker.epoch()

        def failed_assertion():
            assert broker.epoch() == epoch + 1 and not broker.is_open()
            raise RuntimeError("synthetic-verification-failure")

        with pytest.raises(RuntimeError, match="synthetic-verification-failure"):
            broker.open_epoch(failed_assertion)
        assert not broker.db.in_transaction
        assert broker.epoch() == epoch + 1 and not broker.is_open()
        assert dict(broker._permit(permit)) == saved
        assert joined.token_valid(token, "RT")["reason"] == "stale_epoch"
        assert broker.epoch() == epoch + 1 and not broker.is_open() and queued == {permit}
        assert joined.acquire("mcs.extract", "BACKLOG")["reason"] == "epoch_closed"
    finally:
        joined.close()


def test_trusted_assertion_can_check_tokens_outside_a_transaction(broker):
    permit, queued = _queued_request(broker, "sent")
    token = broker._permit(permit)["token"]
    _legacy_owner_fence(broker, permit)
    epoch = broker.epoch()

    def verify_empty():
        assert broker.epoch() == epoch + 1 and not broker.is_open()
        assert not broker.token_valid(token, "RT")["ok"]
        assert not broker.db.in_transaction
        return not queued

    assert not broker.open_epoch(verify_empty)
    assert broker.epoch() == epoch + 1 and not broker.is_open()
    queued.clear()
    assert broker.open_epoch(verify_empty)


def test_new_unsafe_fence_during_trusted_assertion_prevents_reopening(broker):
    joined = adm.Broker(broker.path)
    try:
        permit = broker.acquire("gbrain.query", "RT")["permit_id"]
        token = broker.sent(permit)["token"]
        queued, epoch = set(), broker.epoch()

        def verify_empty():
            observed_empty = not queued
            assert not broker.db.in_transaction
            # A legacy writer commits after the caller's empty observation.
            queued.add(permit)
            _legacy_owner_fence(joined, permit)
            return observed_empty

        assert not broker.open_epoch(verify_empty)
        assert broker.epoch() == joined.epoch() == epoch + 1
        assert not broker.is_open() and not joined.is_open() and queued == {permit}
        assert joined.token_valid(token, "RT")["reason"] == "stale_epoch"
        assert not broker.open_epoch(lambda: not queued)
        queued.clear()
        assert broker.open_epoch(lambda: not queued)
    finally:
        joined.close()


def test_safe_unsent_fence_does_not_interrupt_healthy_shared_work(broker, monkeypatch):
    permit = broker.acquire("gbrain.query", "RT")["permit_id"]
    _expired_owner(broker, permit, monkeypatch, "dead")
    background = broker.acquire("mcs.extract", "BACKLOG")["permit_id"]
    token = broker.sent(background)["token"]
    waiting = broker.acquire("gbrain.query", "RT")["permit_id"]
    saved, epoch = dict(broker._permit(permit)), broker.epoch()
    assert saved["proof"] == "owner_dead_unsent"
    joined = adm.Broker(broker.path)
    try:
        assert joined.epoch() == epoch and joined.is_open()
        assert dict(joined._permit(permit)) == saved
        assert joined.token_valid(token, "BACKLOG")["ok"]
        assert joined.poll(waiting)["state"] == "waiting"
        assert broker.terminal(background, "confirmed_retired", proof="synthetic-server-ack")["terminal"]
        assert joined.poll(waiting)["state"] == "admitted"
        token = joined.sent(waiting)["token"]
        assert broker.token_valid(token, "RT")["ok"] and broker.epoch() == epoch
    finally:
        joined.close()
