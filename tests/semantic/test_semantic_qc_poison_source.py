"""Malformed current extraction is terminal for QC without model calls or reseed loops."""
import time

import pytest

import semantic
import semantic_qc as qc
import semantic_runtime as runtime
from semantic_testkit import _cfg, _FakeJev
from extract_testkit import _extract_artifact, _hash, _ledger, _message


@pytest.mark.parametrize("source", ["invalid_container", "unreadable_integer"])
def test_bad_qc_source_fails_without_calls_or_reseed(tmp_path, source):
    ledger = _ledger(tmp_path)
    try:
        ledger.save_messages([_message(body="SYNTH", posted_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))])
        aid = _extract_artifact(ledger, 1, {"meds": 7}, _hash(ledger))
        if source == "unreadable_integer":
            ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                              ('{"synthetic_unused":' + '9' * 5000 + '}', aid))
            ledger.db.commit()
        assert qc._qc_seed(ledger, time.time()) == 1
        job = ledger.db.execute("SELECT * FROM fetch_jobs WHERE kind=?", (qc.QC_JOB_KIND,)).fetchone()
        cfg, _ = semantic.semantic_config(_cfg(extract_qc="annotate"))
        jev = _FakeJev()
        assert qc._process_qc_job(ledger, cfg, job, jev, time.monotonic() + 60) == "failed"
        assert jev.requests_made == 0
        assert runtime.transition(ledger, runtime.JobToken.from_row(job), "failed")
        assert qc._qc_seed(ledger, time.time()) == 0
        assert ledger.artifacts(qc.QC_ARTIFACT) == []
    finally:
        ledger.close()
