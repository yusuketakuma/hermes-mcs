"""Synthetic checks for the hash-chained patient access log."""
import hashlib
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


def _legacy_line(tmp_path, extra=None):
    folder = tmp_path / access_log.DIR_NAME
    folder.mkdir(mode=0o700)
    path = folder / "access-2026-09.jsonl"
    entry = {"v": 1, "at": "2026-09-21T14:13:20Z", "ts": 1_790_000_000,
             "via": "slack", "actor": "slack:T1:U1", "channel": "C1", "op": "read",
             "kind": "timeline", "project_id": 7, "outcome": "ok", "query": False,
             "prev": access_log.GENESIS}
    entry.update(extra or {})
    line = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(line + b"\n")
    os.chmod(path, 0o600)
    return path, line


def test_legacy_log_checkpoint_survives_new_format_append(tmp_path):
    path, old_line = _legacy_line(tmp_path)
    checkpoint = (1, hashlib.sha256(old_line).hexdigest())
    assert access_log.verify(tmp_path, checkpoint=checkpoint)["ok"] is True
    new_entry = _entry(tmp_path, now=1_792_700_000, patients=[1, 2])
    assert new_entry["prev"] == checkpoint[1]
    assert path.read_bytes() == old_line + b"\n"
    result = access_log.verify(tmp_path, checkpoint=checkpoint)
    assert (result["ok"], result["files"], result["records"]) == (True, 2, 2)


@pytest.mark.parametrize("extra", [{"patients": []}, {"patients_more": 0}, {"unexpected": 1}])
def test_verify_rejects_partial_and_unknown_legacy_fields(tmp_path, extra):
    path, _ = _legacy_line(tmp_path, extra)
    assert access_log.verify(tmp_path)["broken_at"] == {"file": path.name, "line": 1}


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


@pytest.mark.parametrize("next_time", [1_790_000_060, 1_792_700_000])
def test_unterminated_legacy_record_keeps_its_checkpoint(tmp_path, next_time):
    path, old_line = _legacy_line(tmp_path)
    path.write_bytes(old_line)
    checkpoint = (1, hashlib.sha256(old_line).hexdigest())
    assert access_log.verify(tmp_path, checkpoint=checkpoint)["ok"] is True
    _entry(tmp_path, now=next_time)
    assert path.read_bytes().startswith(old_line)
    if next_time == 1_792_700_000:
        assert path.read_bytes() == old_line
    result = access_log.verify(tmp_path, checkpoint=checkpoint)
    assert result["ok"] is True and result["records"] == 2


@pytest.mark.parametrize("next_time", [1_790_000_060, 1_792_700_000])
def test_interrupted_append_is_preserved_and_blocks_new_answers(tmp_path, next_time):
    _entry(tmp_path, now=1_790_000_000)
    path, = (tmp_path / access_log.DIR_NAME).glob("access-*.jsonl")
    complete = path.read_bytes()
    with open(path, "ab") as stream:
        stream.write(b'{"v":1,"at":"half')  # a crash mid-write left no newline
    damaged = path.read_bytes()
    with pytest.raises(access_log.AccessLogError, match="access_log_corrupt"):
        _entry(tmp_path, now=next_time)
    assert path.read_bytes() == damaged and damaged.startswith(complete)
    assert access_log.verify(tmp_path)["ok"] is False


def test_short_write_is_preserved_and_blocks_the_next_append(tmp_path, monkeypatch):
    _entry(tmp_path, now=1_790_000_000)
    path, = (tmp_path / access_log.DIR_NAME).glob("access-*.jsonl")
    complete = path.read_bytes()
    real_write = os.write
    monkeypatch.setattr(access_log.os, "write", lambda fd, data: real_write(fd, data[:10]))
    with pytest.raises(access_log.AccessLogError, match="access_log_write_incomplete"):
        _entry(tmp_path, now=1_790_000_120)
    monkeypatch.setattr(access_log.os, "write", real_write)
    damaged = path.read_bytes()
    assert damaged.startswith(complete) and len(damaged) == len(complete) + 10
    with pytest.raises(access_log.AccessLogError, match="access_log_corrupt"):
        _entry(tmp_path, now=1_790_000_180)
    assert path.read_bytes() == damaged


def test_fsync_failure_keeps_the_complete_unconfirmed_record(tmp_path, monkeypatch):
    _entry(tmp_path, now=1_790_000_000)
    path, = (tmp_path / access_log.DIR_NAME).glob("access-*.jsonl")
    complete = path.read_bytes()
    real_fsync = os.fsync
    monkeypatch.setattr(access_log.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError()))
    with pytest.raises(access_log.AccessLogError, match="access_log_unavailable"):
        _entry(tmp_path, now=1_790_000_060)
    monkeypatch.setattr(access_log.os, "fsync", real_fsync)
    assert path.read_bytes().startswith(complete) and len(path.read_bytes()) > len(complete)
    assert access_log.verify(tmp_path)["records"] == 2


@pytest.mark.parametrize("failure_at", [1, 2, 3])
def test_retry_syncs_a_file_created_by_an_unconfirmed_first_append(tmp_path, monkeypatch, failure_at):
    real_fsync = os.fsync
    attempts = []

    def fail_selected(fd):
        attempts.append(fd)
        if len(attempts) == failure_at:
            raise OSError("synthetic fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(access_log.os, "fsync", fail_selected)
    with pytest.raises(access_log.AccessLogError, match="access_log_unavailable"):
        _entry(tmp_path, now=1_790_000_000)

    synced = []

    def trace_fsync(fd):
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(access_log.os, "fsync", trace_fsync)
    _entry(tmp_path, now=1_790_000_060)
    assert synced == [False, True, True]
    assert access_log.verify(tmp_path)["records"] == 2


def test_unterminated_valid_json_with_broken_chain_is_not_appended(tmp_path):
    path, old_line = _legacy_line(tmp_path)
    damaged = old_line.replace(access_log.GENESIS.encode(), b"f" * 64)
    path.write_bytes(damaged)
    with pytest.raises(access_log.AccessLogError, match="access_log_corrupt"):
        _entry(tmp_path, now=1_790_000_060)
    assert path.read_bytes() == damaged
