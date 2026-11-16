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


def test_a_clock_set_back_keeps_appending_after_the_newest_month(tmp_path):
    december, november = 1_796_083_200, 1_796_083_200 - 3600  # 2026-12-01 / 11-30 UTC
    _entry(tmp_path, now=december)
    late = _entry(tmp_path, now=november)
    _entry(tmp_path, now=december + 60)
    assert late["at"].startswith("2026-11-30")
    assert [p.name for p in (tmp_path / access_log.DIR_NAME).glob("access-*.jsonl")] == [
        "access-2026-12.jsonl"]
    assert access_log.verify(tmp_path)["ok"] is True


def test_an_interrupted_append_is_dropped_before_the_next_record(tmp_path, monkeypatch):
    _entry(tmp_path, now=1_790_000_000)
    path, = (tmp_path / access_log.DIR_NAME).glob("access-*.jsonl")
    complete = path.read_bytes()
    with open(path, "ab") as stream:
        stream.write(b'{"v":1,"at":"half')  # a crash mid-write left no newline
    _entry(tmp_path, now=1_790_000_060)
    assert path.read_bytes().startswith(complete) and b"half" not in path.read_bytes()
    assert access_log.verify(tmp_path)["records"] == 2 and len(list(access_log.read(tmp_path))) == 2

    real_write = os.write
    monkeypatch.setattr(access_log.os, "write", lambda fd, data: real_write(fd, data[:10]))
    with pytest.raises(access_log.AccessLogError, match="access_log_write_incomplete"):
        _entry(tmp_path, now=1_790_000_120)
    monkeypatch.setattr(access_log.os, "write", real_write)
    assert access_log.verify(tmp_path) == {**access_log.verify(tmp_path), "ok": True, "records": 2}


def test_an_interrupted_append_at_month_end_is_dropped_before_the_next_month(tmp_path):
    november, december = 1_796_083_200 - 3600, 1_796_083_200  # 2026-11-30 / 12-01 UTC
    _entry(tmp_path, now=november)
    path, = (tmp_path / access_log.DIR_NAME).glob("access-*.jsonl")
    complete = path.read_bytes()
    with open(path, "ab") as stream:
        stream.write(b'{"v":1,"at":"half')  # the crash came before the month changed
    _entry(tmp_path, now=december)
    assert path.read_bytes() == complete
    assert access_log.verify(tmp_path)["ok"] is True
    assert len(list(access_log.read(tmp_path))) == 2
