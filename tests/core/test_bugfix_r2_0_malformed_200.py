"""An HTTP 200 with an unusable body proves the backend finished — the
permit must be retired as done, never left ``unknown`` holding a slot."""
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


@pytest.mark.parametrize("raw", [b'{"choices": []}', b"not json",
                                 b'{"choices": [{"message": 1}]}'])
def test_malformed_200_chat_retires_permit(broker_path, raw):
    import local_llm
    err = {}
    resp = local_llm.admitted_chat(
        "gbrain.query", "p", error_out=err,
        request_fn=lambda *a: (200, {}, raw))
    assert resp is None and err["kind"] == "protocol"
    b = local_llm._broker(broker_path)
    row = b.db.execute("SELECT state,outcome FROM permits").fetchone()
    assert row["state"] == "terminal" and row["outcome"] == "done"
    assert b.acquire("mcs.extract", "BACKLOG")["admitted"]


def test_malformed_200_probe_retires_permit(broker_path):
    import local_llm
    mode = local_llm.admitted_probe_format(
        "mcs.extract", local_llm.ENDPOINT, "m", None,
        request_fn=lambda *a: (200, {}, b'{"choices": []}'))
    assert mode == "plain"
    b = local_llm._broker(broker_path)
    row = b.db.execute("SELECT state,outcome FROM permits").fetchone()
    assert row["state"] == "terminal" and row["outcome"] == "done"
