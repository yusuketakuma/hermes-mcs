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


@pytest.mark.parametrize("checkpoint", [(-1, access_log.GENESIS), (True, access_log.GENESIS),
                                        (0, "z" * 64), (0,), "invalid"])
def test_invalid_checkpoint_is_rejected(tmp_path, checkpoint):
    with pytest.raises(ValueError, match="checkpoint_invalid"):
        access_log.verify(tmp_path, checkpoint=checkpoint)


def test_zero_record_checkpoint_requires_genesis(tmp_path):
    assert access_log.verify(tmp_path, checkpoint=(0, access_log.GENESIS))["ok"] is True
    assert access_log.verify(tmp_path, checkpoint=(0, "f" * 64))["checkpoint"] == "changed"


def test_short_tail_reads_are_completed_before_hashing(tmp_path, monkeypatch):
    _entry(tmp_path, now=1_790_000_000)
    before = access_log.verify(tmp_path)
    real_pread = os.pread
    monkeypatch.setattr(access_log.os, "pread",
                        lambda fd, size, offset: real_pread(fd, min(size, 8), offset))
    entry = _entry(tmp_path, now=1_790_000_060)
    assert entry["prev"] == before["head"]
    assert access_log.verify(tmp_path)["records"] == 2


def test_early_eof_in_tail_read_keeps_log_unchanged(tmp_path, monkeypatch):
    _entry(tmp_path, now=1_790_000_000)
    path, = (tmp_path / access_log.DIR_NAME).glob("access-*.jsonl")
    before = path.read_bytes()
    monkeypatch.setattr(access_log.os, "pread", lambda fd, size, offset: b"")
    with pytest.raises(access_log.AccessLogError, match="access_log_corrupt"):
        _entry(tmp_path, now=1_790_000_060)
    assert path.read_bytes() == before


@pytest.mark.parametrize("next_time", [1_790_000_060, 1_792_700_000])
@pytest.mark.parametrize("tail", [b"\n", b'{"broken":true}\n', b"not json\n", b"x" * 9000 + b"\n"])
def test_invalid_completed_tail_is_not_appended(tmp_path, tail, next_time):
    _entry(tmp_path, now=1_790_000_000)
    path, = (tmp_path / access_log.DIR_NAME).glob("access-*.jsonl")
    with open(path, "ab") as stream:
        stream.write(tail)
    damaged = path.read_bytes()
    with pytest.raises(access_log.AccessLogError, match="access_log_corrupt"):
        _entry(tmp_path, now=next_time)
    assert path.read_bytes() == damaged


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


@pytest.mark.parametrize("next_time", [1_790_000_060, 1_792_700_000])
@pytest.mark.parametrize("terminated", [True, False])
@pytest.mark.parametrize("actor", ["slack:T1:U1", "a" * 9000])
def test_a_legacy_record_keeps_its_bytes_and_checkpoint(tmp_path, next_time, terminated, actor):
    path, old_line = _legacy_line(tmp_path, {"actor": actor})
    if not terminated:
        path.write_bytes(old_line)
    checkpoint = (1, hashlib.sha256(old_line).hexdigest())
    assert access_log.verify(tmp_path, checkpoint=checkpoint)["ok"] is True
    entry = _entry(tmp_path, now=next_time, patients=[1, 2])
    assert entry["prev"] == checkpoint[1]
    same_month = next_time == 1_790_000_060
    if same_month:   # only the newline the older writer left out is added
        assert path.read_bytes().startswith(old_line + b"\n")
    else:
        assert path.read_bytes() == old_line + (b"\n" if terminated else b"")
    result = access_log.verify(tmp_path, checkpoint=checkpoint)
    assert (result["ok"], result["records"]) == (True, 2)


@pytest.mark.parametrize("extra", [{"patients": []}, {"patients_more": 0}, {"unexpected": 1}])
def test_verify_rejects_partial_and_unknown_legacy_fields(tmp_path, extra):
    path, _ = _legacy_line(tmp_path, extra)
    assert access_log.verify(tmp_path)["broken_at"] == {"file": path.name, "line": 1}
    with pytest.raises(access_log.AccessLogError, match="access_log_corrupt"):
        _entry(tmp_path, now=1_790_000_060)


@pytest.mark.parametrize("next_time", [1_790_000_060, 1_792_700_000])
@pytest.mark.parametrize("fragment", [b'{"v":1,"at":"half', None])
def test_an_unfinished_tail_is_kept_and_blocks_new_records(tmp_path, monkeypatch, next_time, fragment):
    _entry(tmp_path, now=1_790_000_000)
    path, = (tmp_path / access_log.DIR_NAME).glob("access-*.jsonl")
    complete = path.read_bytes()
    if fragment is None:   # this writer's own append stopped part way
        real_write = os.write
        monkeypatch.setattr(access_log.os, "write", lambda fd, data: real_write(fd, data[:10]))
        with pytest.raises(access_log.AccessLogError, match="access_log_write_incomplete"):
            _entry(tmp_path, now=1_790_000_030)
        monkeypatch.setattr(access_log.os, "write", real_write)
    else:                  # a crash or later damage left the same kind of fragment
        with open(path, "ab") as stream:
            stream.write(fragment)
    damaged = path.read_bytes()
    assert damaged.startswith(complete) and len(damaged) > len(complete)
    with pytest.raises(access_log.AccessLogError, match="access_log_corrupt"):
        _entry(tmp_path, now=next_time)
    assert path.read_bytes() == damaged
    assert access_log.verify(tmp_path)["ok"] is False
    assert access_log.failure(tmp_path)["code"] == "access_log_corrupt"


def test_a_record_whose_fsync_failed_is_kept_and_chained(tmp_path, monkeypatch):
    _entry(tmp_path, now=1_790_000_000)
    path, = (tmp_path / access_log.DIR_NAME).glob("access-*.jsonl")
    complete = path.read_bytes()
    real_fsync, target = os.fsync, (path.stat().st_dev, path.stat().st_ino)

    def fsync(fd):   # only the log file: the directories and the failure note still sync
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == target:
            raise OSError("synthetic")
        return real_fsync(fd)
    monkeypatch.setattr(access_log.os, "fsync", fsync)
    with pytest.raises(access_log.AccessLogError, match="access_log_unavailable"):
        _entry(tmp_path, now=1_790_000_060)
    monkeypatch.setattr(access_log.os, "fsync", real_fsync)
    assert access_log.failure(tmp_path)["code"] == "access_log_unavailable"
    assert path.read_bytes().startswith(complete) and len(path.read_bytes()) > len(complete)
    _entry(tmp_path, now=1_790_000_120)   # the failure note observes; it does not block
    assert access_log.verify(tmp_path)["records"] == 3 and access_log.failure(tmp_path) is None


def test_every_earlier_month_is_synced_before_a_new_month_succeeds(tmp_path, monkeypatch):
    september, october, november = 1_790_000_000, 1_792_700_000, 1_795_300_000
    _entry(tmp_path, now=september)
    _entry(tmp_path, now=october)
    folder = tmp_path / access_log.DIR_NAME
    earlier = {p.stat().st_ino: p.name for p in folder.glob("access-*.jsonl")}
    assert len(earlier) == 2
    real_fsync, synced, failing = os.fsync, [], set()

    def fsync(fd):
        inode = os.fstat(fd).st_ino
        synced.append(inode)
        if inode in failing:
            raise OSError("synthetic")
        return real_fsync(fd)
    monkeypatch.setattr(access_log.os, "fsync", fsync)
    failing.add(min(earlier, key=earlier.get))   # September's earlier fsync never landed
    with pytest.raises(access_log.AccessLogError, match="access_log_unavailable"):
        _entry(tmp_path, now=november)
    assert access_log.failure(tmp_path)["code"] == "access_log_unavailable"
    failing.clear()
    synced.clear()
    _entry(tmp_path, now=november + 60)
    assert set(earlier) <= set(synced) and access_log.failure(tmp_path) is None
    assert access_log.verify(tmp_path)["records"] == 4


def test_new_log_directory_is_made_durable_before_the_first_record(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(access_log, "fsync_dir", lambda path, **_k: synced.append(os.fspath(path)))
    access_log.record(tmp_path, via="slack", actor="a", op="read")
    assert os.fspath(tmp_path) in synced

    def broken(path, **_k):
        raise OSError("synthetic")
    monkeypatch.setattr(access_log, "fsync_dir", broken)
    with pytest.raises(access_log.AccessLogError, match="access_log_unavailable"):
        access_log.record(tmp_path, via="slack", actor="a", op="read")


def test_log_directory_is_synced_again_after_a_failed_publication(tmp_path, monkeypatch):
    real_fsync, synced, fail = os.fsync, [], [True]
    log_dir = tmp_path / access_log.DIR_NAME

    def fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) and log_dir.exists() and info.st_ino == log_dir.stat().st_ino:
            synced.append(fd)
            if fail.pop() if fail else False:
                raise OSError("synthetic")
        return real_fsync(fd)
    monkeypatch.setattr(access_log.os, "fsync", fsync)
    with pytest.raises(access_log.AccessLogError):
        access_log.record(tmp_path, via="slack", actor="a", op="read")
    access_log.record(tmp_path, via="slack", actor="a", op="read")
    assert len(synced) == 2  # the retry synced the log directory again


def test_a_failed_append_leaves_a_content_free_note_until_a_record_succeeds(tmp_path):
    folder = tmp_path / access_log.DIR_NAME
    folder.mkdir(mode=0o755)
    os.chmod(folder, 0o755)
    for at in (1_790_000_000, 1_790_000_600):
        with pytest.raises(access_log.AccessLogError, match="access_log_untrusted"):
            _entry(tmp_path, now=at)
    assert access_log.failure(tmp_path) == {"code": "access_log_untrusted",
                                            "since": 1_790_000_000, "last": 1_790_000_600}
    note = tmp_path / access_log.FAILURE_NAME
    assert stat.S_IMODE(note.stat().st_mode) == 0o600
    assert not {"slack", "U1", "C1", "7"} & set(json.loads(note.read_text()).values())
    os.chmod(folder, 0o700)
    _entry(tmp_path)
    assert access_log.failure(tmp_path) is None and not note.exists()


@pytest.mark.parametrize("kind", ["not_json", "shared_mode", "link"])
def test_an_unreadable_note_still_reports_a_failure(tmp_path, kind):
    assert access_log.failure(tmp_path) is None
    note = tmp_path / access_log.FAILURE_NAME
    if kind == "link":
        (tmp_path / "elsewhere.json").write_text('{"code": "access_log_unavailable"}')
        note.symlink_to(tmp_path / "elsewhere.json")
    else:
        note.write_text("not json" if kind == "not_json" else '{"code": "access_log_unavailable"}')
        note.chmod(0o600 if kind == "not_json" else 0o644)
    assert access_log.failure(tmp_path) == {"code": "unknown", "since": None, "last": None}


def test_a_failure_note_that_cannot_be_written_keeps_the_original_error(tmp_path):
    with pytest.raises(access_log.AccessLogError, match="access_log_unavailable"):
        _entry(tmp_path / "absent")
    assert not (tmp_path / "absent").exists()
