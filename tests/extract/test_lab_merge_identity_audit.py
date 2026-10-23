"""Keep distinct source-bound laboratory readings across extraction chunks."""
import json

import pytest

import extract_validation
import rollup
from extract_testkit import _hash, _ledger, _message


def _chunk(value, unit="mg/dL", quote=None, **attributes):
    quote = quote or f"Cr {value}{unit}"
    raw = {"name": "Cr", "value": value, "unit": unit,
           "subject": "patient", "status": "current", "evidence": quote, **attributes}
    return extract_validation.validate({"labs": [raw]}, quote)


@pytest.mark.parametrize("second", [
    pytest.param(_chunk(2.1), id="different-value"),
    pytest.param(_chunk(1.2, "μmol/L"), id="different-unit"),
    pytest.param(_chunk(1.2, quote="再検 Cr 1.2mg/dL"), id="different-evidence"),
    pytest.param(_chunk(1.2, quote="Cr 1.2mg/dL↑", flag="high"), id="different-flag"),
    pytest.param(_chunk(1.2, status="past"), id="different-time-scope"),
    pytest.param(_chunk(1.2, subject="family"), id="different-subject"),
])
def test_merge_retains_distinct_laboratory_reports(second):
    first = _chunk(1.2)
    assert len(first["labs"]) == len(second["labs"]) == 1
    assert first["labs"][0] != second["labs"][0]
    assert extract_validation.merge([first, second])["labs"] == first["labs"] + second["labs"]


def test_merge_deduplicates_only_identical_laboratory_reports():
    first = _chunk(1.2)
    reordered = {"labs": [dict(reversed(list(first["labs"][0].items())))]}
    assert extract_validation.merge([first, reordered])["labs"] == first["labs"]


def test_undated_chunk_readings_do_not_become_one_current_lab(tmp_path):
    chunks = [_chunk(2.1), _chunk(1.2)]
    merged = extract_validation.merge(chunks)
    body = "Cr 2.1mg/dL。\nCr 1.2mg/dL。"
    store = _ledger(tmp_path)
    try:
        store.ensure_patient(1)
        store.save_messages([_message(mid=1, body=body, posted_at="2026-10-11T09:00:00+09:00")])
        store.artifact_add("extract_llm", json.dumps(merged), project_id=1, message_id=1,
                           meta={"hash": _hash(store, 1)})
        assert not rollup.build_rollup(store.db, 1).get("recent_labs")
        assert [lab["value"] for lab in merged["labs"]] == [2.1, 1.2]
    finally:
        store.close()
