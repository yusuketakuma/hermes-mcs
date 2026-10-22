"""Synthetic attachment integrity checks: temp DB and files only, nothing repaired."""
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing

import pytest

import attachment_audit as aa
import mcs_repair

GENERATION = "00000000-0000-4000-8000-000000000029"


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "attachments"
    root.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    rows = []

    def add(aid, *, data=None, size=None, local=None, state="downloaded"):
        if data is not None:
            (root / str(aid)).write_bytes(data)
        rows.append((aid, 1000 + aid, str(local or root / str(aid)),
                     len(data or b"") if size is None else size, state))

    add(1, data=b"abc")                       # ok
    add(2, size=3)                            # missing
    add(3, data=b"ab", size=3)                # size_mismatch
    add(4, size=3)                            # symlink -> not_regular
    (root / "4").symlink_to(root / "1")
    add(5, size=0)                            # FIFO -> not_regular
    os.mkfifo(root / "5")
    add(6, data=b"abc", local=other / "6")    # outside the root -> path_mismatch
    add(7, data=b"abc")                       # bytes NULL -> record_invalid
    add(8, size=3)                            # directory -> not_regular
    (root / "8").mkdir()
    add(9, state="pending", size=3)           # not downloaded -> out of scope
    add(10, data=b"")                         # empty file attachment -> ok
    snapshot = tmp_path / "synthetic snapshot.db"
    with closing(sqlite3.connect(snapshot)) as db:
        db.executescript("""
          CREATE TABLE attachments(attachment_id INTEGER PRIMARY KEY, message_id INTEGER,
            name TEXT, url TEXT, local_path TEXT, bytes INTEGER, sha256 TEXT, state TEXT);
          CREATE TABLE snapshot_meta(singleton INTEGER PRIMARY KEY, generation_id TEXT,
            generated_at REAL);
        """)
        db.executemany("INSERT INTO attachments(attachment_id,message_id,local_path,bytes,state,"
                       "name,url) VALUES(?,?,?,?,?,'synthetic.pdf','https://example.invalid/f')",
                       rows)
        db.execute("UPDATE attachments SET bytes=NULL WHERE attachment_id=7")
        db.execute("INSERT INTO snapshot_meta VALUES(1,?,100)", (GENERATION,))
        db.commit()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    return {"snapshot": snapshot, "root": root, "private": private}


def _tree(root):
    return {p.name: (p.is_symlink(), p.lstat().st_mode, p.lstat().st_size)
            for p in root.iterdir()}


def test_each_downloaded_row_gets_one_fixed_reason(world):
    before, files = _hash(world["snapshot"]), _tree(world["root"])
    summary, private = aa.audit(world["snapshot"], world["root"])
    assert summary["counts"] == {"missing": 1, "size_mismatch": 1, "unreadable": 0,
                                 "not_regular": 3, "path_mismatch": 1, "record_invalid": 1}
    assert (summary["checked"], summary["ok"], summary["unchecked"]) == (9, 2, 0)
    assert summary["partial"] is False and summary["stop_reason"] is None
    assert (summary["refetch_candidates"], summary["review_candidates"]) == (2, 5)
    reasons = {c["attachment_id"]: c["reason"] for c in private["candidates"]}
    assert reasons == {2: "missing", 3: "size_mismatch", 4: "not_regular",
                       5: "not_regular", 6: "path_mismatch", 7: "record_invalid",
                       8: "not_regular"}
    assert private["plan"]["refetch"] == [2, 3]
    assert private["plan"]["operator_review"] == [4, 5, 6, 7, 8]
    assert next(c for c in private["candidates"] if c["attachment_id"] == 3)[
        "observed_bytes"] == 2
    # bound to the exact snapshot, and nothing read was changed
    assert summary["snapshot_sha256"] == before == _hash(world["snapshot"])
    assert summary["snapshot_binding"]["generated_at"] == 100
    assert _tree(world["root"]) == files
    # the summary never carries ids
    assert not {"candidates", "next_after_id", "plan", "after_id"} & summary.keys()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
def test_permission_denied_is_unreadable_not_missing(world):
    path = world["root"] / "1"
    path.chmod(0)
    try:
        _, private = aa.audit(world["snapshot"], world["root"])
    finally:
        path.chmod(0o600)
    assert {c["attachment_id"]: c["reason"] for c in private["candidates"]}[1] == "unreadable"


def test_limit_stops_partially_and_the_cursor_resumes(world):
    first, first_private = aa.audit(world["snapshot"], world["root"], limit=4)
    assert (first["checked"], first["unchecked"]) == (4, 5)
    assert first["partial"] is True and first["stop_reason"] == "limit"
    assert first_private["next_after_id"] == 4
    rest, rest_private = aa.audit(world["snapshot"], world["root"],
                                  after_id=first_private["next_after_id"])
    assert (rest["checked"], rest["partial"], rest_private["next_after_id"]) == (5, False, None)
    seen = [c["attachment_id"] for c in first_private["candidates"] + rest_private["candidates"]]
    assert seen == [2, 3, 4, 5, 6, 7, 8]


def test_time_budget_stops_the_scan_partially_without_guessing(world, monkeypatch):
    now, calls, real = {"t": 0.0}, [], aa._check

    def check(*args):
        calls.append(1)
        if len(calls) == 2:
            now["t"] = 5.0  # past the scan deadline after the second file
        return real(*args)

    monkeypatch.setattr(aa, "_check", check)
    summary, private = aa.audit(world["snapshot"], world["root"], max_seconds=1,
                                clock=lambda: now["t"])
    assert (summary["checked"], summary["stop_reason"], private["next_after_id"]) == (2, "time", 2)
    assert summary["unchecked"] == 7
    assert summary["counts"]["missing"] == 1


def _clock(*values, then):
    """Return the given monotonic readings in order, then ``then`` forever."""
    readings = iter(values)
    return lambda: next(readings, then)


def test_deadline_inside_sql_leaves_the_unchecked_count_unknown(world):
    with closing(sqlite3.connect(world["snapshot"])) as db:
        db.executemany("INSERT INTO attachments(attachment_id,local_path,bytes,state) "
                       "VALUES(?,'x',1,'downloaded')", [(i,) for i in range(100, 5100)])
        db.commit()
    # hash deadline, hash chunk, scan deadline; then every reading is late
    summary, private = aa.audit(world["snapshot"], world["root"], max_seconds=1,
                                clock=_clock(0.0, 0.0, 0.0, then=100.0))
    assert (summary["checked"], summary["partial"], summary["stop_reason"]) == (0, True, "time")
    assert summary["unchecked"] is None
    assert sum(summary["counts"].values()) == 0 and private["candidates"] == []


def test_snapshot_hash_over_its_deadline_refuses_the_result(world):
    with pytest.raises(ValueError, match="time_budget_exceeded"):
        aa.audit(world["snapshot"], world["root"], max_seconds=1,
                 clock=_clock(0.0, then=5.0))


def test_ancestor_swapped_for_a_symlink_mid_walk_is_not_followed(world, tmp_path, monkeypatch):
    parent = tmp_path / "swap"
    (parent / "attachments").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "attachments").mkdir(parents=True)
    real, opened, swapped = os.open, [], []

    def racing_open(path, flags, *args, dir_fd=None, **kwargs):
        if path == "swap" and dir_fd is not None and not swapped:
            # the parent was a real directory until the walk reached it
            parent.rename(tmp_path / "swap-real")
            parent.symlink_to(outside)
            swapped.append(1)
        if dir_fd is not None:
            opened.append(path)
        return real(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(ValueError, match="attachment_root_refused"):
        aa.audit(world["snapshot"], parent / "attachments")
    assert opened[-1] == "swap" and "attachments" not in opened


@pytest.mark.parametrize("make", ["symlink_root", "symlink_parent", "absent", "file"])
def test_refused_root_classifies_nothing(world, tmp_path, make):
    if make == "symlink_root":
        root = tmp_path / "link"
        root.symlink_to(world["root"])
    elif make == "symlink_parent":
        (tmp_path / "parent-link").symlink_to(tmp_path)
        root = tmp_path / "parent-link" / "attachments"
    elif make == "absent":
        root = tmp_path / "no-such-dir"
    else:
        root = tmp_path / "plain-file"
        root.write_bytes(b"")
    with pytest.raises(ValueError, match="attachment_root_refused"):
        aa.audit(world["snapshot"], root)


def test_live_snapshot_sidecar_is_refused(world):
    (world["snapshot"].parent / (world["snapshot"].name + "-wal")).write_bytes(b"")
    with pytest.raises(ValueError, match="static_snapshot_required"):
        aa.audit(world["snapshot"], world["root"])


@pytest.mark.parametrize("bounds", [{"limit": 0}, {"limit": aa.MAX_LIMIT + 1},
                                    {"after_id": -1}, {"max_seconds": 0},
                                    {"max_seconds": aa.MAX_SECONDS + 1}, {"limit": True},
                                    {"after_id": 2**63}, {"after_id": True}])
def test_bounds_are_validated(world, bounds):
    with pytest.raises(ValueError, match="attachment_audit_bounds_invalid"):
        aa.audit(world["snapshot"], world["root"], **bounds)


def test_cli_prints_counts_and_writes_ids_only_to_the_private_file(world, capsys):
    out = world["private"] / "attachments.json"
    argv = ["attachments", "--snapshot", str(world["snapshot"]),
            "--attachment-root", str(world["root"]), "--limit", "4", "--out", str(out)]
    assert mcs_repair.main(argv) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["partial"] is True and "candidates" not in printed
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    saved = json.loads(out.read_text())
    assert saved["next_after_id"] == 4 and saved["plan"]["refetch"] == [2, 3]
    assert saved["snapshot_sha256"] == printed["snapshot_sha256"]
    # never overwrite an existing private result
    assert mcs_repair.main(argv) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "repair_input_or_evidence_refused"


def test_cli_refuses_a_shared_output_directory(world, tmp_path, capsys):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    argv = ["attachments", "--snapshot", str(world["snapshot"]),
            "--attachment-root", str(world["root"]), "--out", str(shared / "a.json")]
    assert mcs_repair.main(argv) == 1
    assert not (shared / "a.json").exists()
    assert "1002" not in capsys.readouterr().out
