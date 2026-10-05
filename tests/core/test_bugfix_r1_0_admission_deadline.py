"""An already-expired deadline must retire the permit as not_sent, never
leave it ``unknown`` occupying a BACKLOG slot for good."""
import time
from contextlib import suppress

import pytest

import llm_admission as adm


@pytest.fixture
def broker_path(tmp_path, monkeypatch):
    import local_llm
    path = str(tmp_path / "adm.db")
    b = adm.Broker(path)
    b.open_epoch(lambda: True)
    b.close()
    monkeypatch.setenv("MCS_LLM_ADMISSION", path)
    local_llm._BROKERS.clear()
    yield path
    for b in local_llm._BROKERS.values():
        with suppress(Exception):
            b.close()
    local_llm._BROKERS.clear()


def _expired_send(endpoint, method, body, timeout, deadline):
    # mirrors bounded_http's pre-send deadline rejection
    raise TimeoutError("http worker deadline exceeded")


def test_expired_deadline_chat_frees_slot(broker_path):
    import local_llm
    for _ in range(local_llm.SLOT_COUNT):
        resp = local_llm.admitted_chat(
            "mcs.extract", "p", endpoint=local_llm.ENDPOINT,
            deadline=time.monotonic() - 0.001, request_fn=_expired_send)
        assert resp["admission"] == "deadline" and resp["text"] is None
    b = local_llm._broker(broker_path)
    assert b.status()["occupying"]["BACKLOG"] == 0
    assert b.acquire("mcs.extract", "BACKLOG")["admitted"]


def test_expired_deadline_probe_frees_slot(broker_path):
    import local_llm
    mode = local_llm.admitted_probe_format(
        "mcs.extract", local_llm.ENDPOINT, "m", None,
        deadline=time.monotonic() - 0.001, request_fn=_expired_send)
    assert mode == "plain"
    b = local_llm._broker(broker_path)
    row = b.db.execute("SELECT state,outcome FROM permits").fetchone()
    assert row["state"] == "terminal" and row["outcome"] == "not_sent"
