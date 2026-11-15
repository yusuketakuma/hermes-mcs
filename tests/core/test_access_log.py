"""Synthetic checks for the hash-chained patient access log."""
import json
import os
import stat

import pytest

import access_log


def _entry(tmp_path, **fields):
    base = {"via": "slack", "actor": "slack:T1:U1", "op": "read", "kind": "timeline",
            "project_id": 7, "channel": "C1"}
    return access_log.record(tmp_path, **{**base, **fields})


def test_records_only_identifiers_with_private_modes(tmp_path):
    entry = _entry(tmp_path, query=True, now=1_790_000_000)
    folder = tmp_path / access_log.DIR_NAME
    files = list(folder.glob("access-*.jsonl"))
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert [stat.S_IMODE(f.stat().st_mode) for f in files] == [0o600]
    stored = json.loads(files[0].read_text())
    assert stored == entry and set(stored) == set(access_log.FIELDS)
    assert stored["prev"] == access_log.GENESIS and stored["query"] is True
    assert stored["at"].endswith("Z") and stored["project_id"] == 7


def test_chain_spans_months_and_detects_edits_and_removals(tmp_path):
    _entry(tmp_path, now=1_790_000_000)          # 2026-09
    _entry(tmp_path, now=1_792_700_000, outcome="user_not_allowed")  # 2026-10
    _entry(tmp_path, now=1_792_700_100)
    intact = access_log.verify(tmp_path)
    assert (intact["ok"], intact["files"], intact["records"]) == (True, 2, 3)
    newest = sorted((tmp_path / access_log.DIR_NAME).glob("access-*.jsonl"))[-1]
    lines = newest.read_bytes().splitlines(keepends=True)
    newest.write_bytes(lines[0].replace(b'"project_id":7', b'"project_id":8') + lines[1])
    assert access_log.verify(tmp_path)["broken_at"] == {"file": newest.name, "line": 2}
    oldest = sorted((tmp_path / access_log.DIR_NAME).glob("access-*.jsonl"))[0]
    os.unlink(oldest)
    assert access_log.verify(tmp_path)["ok"] is False


def test_read_filters_by_time_and_missing_log_is_empty(tmp_path):
    assert list(access_log.read(tmp_path)) == []
    assert access_log.verify(tmp_path) == {"ok": True, "files": 0, "records": 0,
                                           "head": access_log.GENESIS}
    _entry(tmp_path, now=1_790_000_000)
    _entry(tmp_path, now=1_790_000_500)
    assert [e["ts"] for e in access_log.read(tmp_path, since=1_790_000_100)] == [1_790_000_500]


def test_untrusted_directory_and_missing_root_fail_closed(tmp_path):
    folder = tmp_path / access_log.DIR_NAME
    folder.mkdir(mode=0o755)
    os.chmod(folder, 0o755)
    with pytest.raises(access_log.AccessLogError, match="access_log_untrusted"):
        _entry(tmp_path)
    with pytest.raises(access_log.AccessLogError, match="access_log_unavailable"):
        _entry(tmp_path / "absent")


def test_free_text_is_bounded_and_ids_must_be_integers(tmp_path):
    entry = _entry(tmp_path, actor="x" * 999, project_id="7")
    assert len(entry["actor"]) == 200 and entry["project_id"] is None


def test_checkpoint_detects_a_dropped_or_edited_newest_record(tmp_path):
    _entry(tmp_path, now=1_790_000_000)
    _entry(tmp_path, now=1_790_000_100)
    mark = access_log.verify(tmp_path)
    checkpoint = (mark["records"], mark["head"])
    _entry(tmp_path, now=1_790_000_200)
    assert access_log.verify(tmp_path, checkpoint=checkpoint)["ok"] is True
    path = next((tmp_path / access_log.DIR_NAME).glob("access-*.jsonl"))
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(lines[0] + lines[1].replace(b'"project_id":7', b'"project_id":9'))
    assert access_log.verify(tmp_path)["ok"] is True  # the chain alone cannot see it
    assert access_log.verify(tmp_path, checkpoint=checkpoint)["checkpoint"] == "changed"
    path.write_bytes(lines[0])
    assert access_log.verify(tmp_path, checkpoint=checkpoint)["checkpoint"] == "records_missing"


def test_free_text_codes_become_invalid_and_patient_lists_are_bounded(tmp_path):
    entry = _entry(tmp_path, op="read", kind="合成 患者 本文", outcome="bad read",
                   patients=[*range(access_log.PATIENT_LIMIT + 5), "9", True])
    assert entry["kind"] == "invalid" and entry["outcome"] == "invalid"
    assert entry["patients"] == list(range(access_log.PATIENT_LIMIT))
    assert entry["patients_more"] == 5
    assert access_log.verify(tmp_path)["ok"] is True
