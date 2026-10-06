"""Unreadable phase values remain evidence without blocking startup recovery."""
import json

import pytest

from adapters.common import journal


@pytest.mark.parametrize("phase", [[], {}])
def test_unhashable_phase_does_not_block_unknown_recovery(tmp_path, phase):
    path = tmp_path / "journal-synthetic.jsonl"
    rows = [{"phase": "begin", "attempt_id": "pending"},
            {"phase": phase, "attempt_id": "pending"},
            {"phase": "result", "attempt_id": "reported", "result": "delivered"},
            {"phase": phase, "attempt_id": "reported"}]
    original = "".join(json.dumps(row) + "\n" for row in rows)
    path.write_text(original)
    records, clean = journal.scan_checked(str(tmp_path))
    assert not clean
    assert journal.unfinished(records)["pending"]["phase"] == "pre_http"
    assert journal.unreported(records)["reported"]["record"]["result"] == "delivered"
    # Even a permissive pruning callback must not drop corrupt evidence.
    assert journal.compact(str(tmp_path), active="", file_ok=lambda _rows: True,
                           prunable=lambda _aid, _rows: True) == 0
    assert path.read_text() == original
