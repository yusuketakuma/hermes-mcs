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


def test_append_after_torn_tail_keeps_the_next_row_readable(tmp_path):
    """Regression: a torn write without a trailing newline swallowed the
    next good row into one unparsable line. The torn line itself still
    marks the scan incomplete — it may be an unknown attempt's witness."""
    journal.append(str(tmp_path), "synthetic", {"phase": "begin", "attempt_id": "a"})
    path = tmp_path / "journal-synthetic.jsonl"
    with open(path, "ab") as handle:
        handle.write(b'{"attempt_id":"b","pha')          # torn by ENOSPC
    journal.append(str(tmp_path), "synthetic", {"phase": "started", "attempt_id": "c"})
    records, clean = journal.scan_checked(str(tmp_path))
    assert not clean
    assert [r["phase"] for r in records["c"]] == ["started"]
    assert path.read_bytes().endswith(b"\n")
