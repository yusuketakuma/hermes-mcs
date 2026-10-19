"""A malformed stored lab report never stops the patient rollup or revives an older value."""
import json

import pytest

import rollup
from extract_testkit import _hash, _ledger, _message


@pytest.mark.parametrize("bad", [{"unit": ["mg/dL"]}, {"evidence": {"text": "Cr 1.0"}}, {"value": [1.0]}])
def test_malformed_newest_lab_blocks_the_slot_without_crashing(tmp_path, bad):
    store = _ledger(tmp_path)
    store.ensure_patient(1)
    for mid, at, value, extra in ((1, "2026-10-01T09:00:00+09:00", "2.1", {}),
                                  (2, "2026-10-10T09:00:00+09:00", "1.0", bad)):
        store.save_messages([_message(mid=mid, body=f"Cr {value}mg/dL。", posted_at=at)])
        lab = {"name": "Cr", "value": value, "unit": "mg/dL", "subject": "patient",
               "evidence": f"Cr {value}mg/dL", **extra}
        store.artifact_add("extract_llm", json.dumps({"labs": [lab]}), project_id=1, message_id=mid,
                           meta={"hash": _hash(store, mid)})
    try:
        labs = rollup.build_rollup(store.db, 1).get("recent_labs")
    finally:
        store.close()
    assert not labs   # the older 2.1 is not revived as the current value
