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


@pytest.mark.parametrize("row", [
    b'{"attempt_id":"synthetic","phase":"started","phase":"begin"}',
    b'{"attempt_id":"hidden","attempt_id":"synthetic","phase":"begin"}',
    b'{"attempt_id":"synthetic","phase":"result","result":"delivered","result":"not_sent"}',
    b'{"attempt_id":"synthetic","phase":"begin","receipt_envelope":{"channel_id":"one","channel_id":"two"}}',
])
def test_duplicate_keys_cannot_prove_unsent_or_be_compacted(tmp_path, row):
    path = tmp_path / "journal-synthetic.jsonl"
    original = b'{"attempt_id":"synthetic","phase":"claimed"}\n' + row + b"\n"
    path.write_bytes(original)
    records, clean = journal.scan_checked(str(tmp_path))
    assert not clean
    assert records["synthetic"] == [{"attempt_id": "synthetic", "phase": "claimed"}]
    cache = journal.ScanCache(str(tmp_path))
    cached = cache.refresh()
    assert list(journal.all_rows(cached)) == records["synthetic"]
    assert journal.compact(str(tmp_path), active="", file_ok=lambda _rows: True,
                           prunable=lambda _aid, _rows: True) == 0
    assert path.read_bytes() == original


@pytest.mark.parametrize("damage", [
    b'{"attempt_id":"a","attempt_id":"b","phase":"started"}\n',
    b'{"attempt_id":"b","phase":',
])
def test_corrupt_journal_prevents_pruning_evidence_in_another_file(tmp_path, damage):
    first = tmp_path / "journal-a.jsonl"
    second = tmp_path / "journal-b.jsonl"
    first.write_bytes(b'{"attempt_id":"a","phase":"claimed"}\n' + damage)
    second.write_bytes(b'{"attempt_id":"b","phase":"receipt","result":"delivered"}\n')
    before = {path: path.read_bytes() for path in (first, second)}
    assert journal.compact(str(tmp_path), active="", file_ok=lambda _rows: True,
                           prunable=lambda _aid, _rows: True) == 0
    assert {path: path.read_bytes() for path in before} == before
