"""Synthetic interrupted preparation must not strand committed extraction claims."""
import pytest

import extract_llm
from extract_testkit import _ledger, _message


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt])
def test_failed_preparation_releases_all_claims_without_discarding_rules(tmp_path, monkeypatch, exception):
    ledger = _ledger(tmp_path)
    ledger.save_messages([_message(mid=i, body=f"SYNTH-{i}") for i in range(1, 4)])
    real = extract_llm._ensure_v1
    prepared = []

    def prepare(db, row, hints):
        if prepared:
            raise exception("synthetic rule preparation interruption")
        real(db, row, hints)
        prepared.append(row["message_id"])

    monkeypatch.setattr(extract_llm, "_ensure_v1", prepare)
    monkeypatch.setattr(extract_llm, "llm_extract", lambda *args, **kwargs: pytest.fail("no model call"))
    try:
        with pytest.raises(exception):
            extract_llm.run_pending(ledger, limit=3, admitted_ids={1, 2, 3})
        assert ledger.db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0] == 0
        assert ledger.artifacts("extract_v1", message_id=prepared[0])
    finally:
        ledger.close()
