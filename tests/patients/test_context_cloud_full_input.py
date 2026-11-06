"""Cloud context must inspect full originals after earlier local progress."""
import pytest

import extract_llm
import extract_llm_prepare
import patient_context_refresh_state as state
from anthropic_context_testkit import cloud, drain, runtime, step
from test_patient_context_refresh import seed

__all__ = ["cloud", "runtime"]


@pytest.mark.parametrize("finish_local", [False, True])
def test_cloud_reads_full_original_after_local_checkpoint(runtime, cloud, monkeypatch, finish_local):
    ledger, _, local_calls = runtime
    body = "完全合成の居住記録。" * 170 + "\n\n" + "完全合成の通院記録。" * 170
    seed(ledger, body=body)
    with monkeypatch.context() as patch:
        patch.setattr(extract_llm, "_cloud_mode", lambda *args: "off")
        patch.setattr(extract_llm_prepare, "_cloud_mode", extract_llm._cloud_mode)
        if finish_local:
            drain(runtime)
        else:
            assert step(runtime)["status"] == "progressed"
    assert local_calls and not cloud[0]
    originals = [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind=? ORDER BY artifact_id", (state.CHUNK,))]
    assert originals
    drain(runtime)
    chunks = [prompt["source"] for prompt in cloud[0] if "source" in prompt]
    assert "".join(chunks) == body, "local cursor is not cloud input coverage"
    for old in originals:
        assert tuple(ledger.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (old[0],)).fetchone()) == old
    count = len(cloud[0])
    assert step(runtime)["status"] == "complete"
    assert len(cloud[0]) == count
