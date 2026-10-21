"""Unknown process identity never creates a permit or abandons an older owner."""
import os
from contextlib import closing

import pytest

import llm_admission as adm


@pytest.mark.parametrize("client,cls", [("mcs.extract", "BACKLOG"), ("gbrain.query", "RT")])
def test_unknown_identity_rejects_without_reservations_then_recovers(tmp_path, monkeypatch, client, cls):
    monkeypatch.setattr(adm, "_proc_started", lambda pid: None)
    with closing(adm.Broker(str(tmp_path / "admission.db"))) as broker:
        broker.open_epoch(lambda: True)
        assert broker.acquire(client, cls)["reason"] == "owner_identity_unavailable"
        assert broker.db.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 0
        assert broker.status()["rt_waiting"] == 0
        monkeypatch.setattr(adm, "_proc_started", lambda pid: "proc:12345")
        acquired = broker.acquire(client, cls)
        assert acquired["admitted"]
        assert broker._permit(acquired["permit_id"])["owner_started"] == "proc:12345"


def test_unknown_identity_does_not_register_rt_waiting_or_release_legacy_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(adm, "_proc_started", lambda pid: None)
    monkeypatch.setattr(adm, "_pid_alive", lambda pid: True)
    with closing(adm.Broker(str(tmp_path / "admission.db"))) as broker:
        broker.open_epoch(lambda: True)
        with broker.db:
            broker.db.execute(
                "INSERT INTO permits(epoch,client,cls,state,created_at,owner_pid) "
                "VALUES(?,'mcs.extract','BACKLOG','unknown',0,?)",
                (broker.epoch(), os.getpid()))
        assert broker.acquire("gbrain.query", "RT")["reason"] == "owner_identity_unavailable"
        assert broker.status()["rt_waiting"] == 0
        assert [row[0] for row in broker.db.execute("SELECT state FROM permits")] == ["unknown"]
        monkeypatch.setattr(adm, "_proc_started", lambda pid: "proc:12345")
        assert broker.acquire("gbrain.query", "RT")["reason"] == "waiting"
        assert broker.status()["rt_waiting"] == 1
        assert [row[0] for row in broker.db.execute(
            "SELECT state FROM permits ORDER BY permit_id")] == ["unknown", "waiting"]
