"""The restore staging copy is bound to the update journal before any
backup byte is copied, and only that exact recorded file is ever cleaned
up. Synthetic temporary databases and journals only."""
import hashlib
import json
import os
import sqlite3
import tempfile
import time

import pytest

import mcs_update
from ops_testkit import _load


def _db(path, rows):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("CREATE TABLE t(v)")
    con.executemany("INSERT INTO t VALUES(?)", [(r,) for r in rows])
    con.commit()
    con.close()


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _rows(path):
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT v FROM t ORDER BY v")]
    finally:
        con.close()


class World:
    def __init__(self, which, tmp_path, monkeypatch):
        self.which = which
        self.mod = mcs_update if which == "update" else _load()
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.live = self.data / "ledger.db"
        self.backup = tmp_path / "backup.db"
        _db(self.live, ["live"])
        _db(self.backup, ["backup"])
        monkeypatch.setattr(self.mod, "LEDGER", str(self.live))
        monkeypatch.setattr(self.mod, "STATE_PATH", str(self.data / "update_state.json"))
        if which == "recover":
            monkeypatch.setattr(self.mod, "DATA", str(self.data))

    def journal(self, applying=True, **extra):
        state = {"v": 1, "stages": [], "applied": [], "attempts": {}, "executed": {},
                 "applying": ({"tag": "v9.9.9", "sha": "b" * 40, "prev_sha": "a" * 40,
                               "rollback": True, "backup_path": str(self.backup),
                               "command_id": "cid-1", "at": time.time(), **extra}
                              if applying else None)}
        (self.data / "update_state.json").write_text(json.dumps(state))

    def state(self):
        return json.loads((self.data / "update_state.json").read_text())

    def replace(self):
        self.mod._replace_database(str(self.backup), _sha(self.backup), lambda: None)

    def staging(self):
        return sorted(p.name for p in self.data.iterdir() if p.name.startswith(".restore."))

    def leftover(self):
        fd, path = tempfile.mkstemp(dir=self.data, prefix=".restore.", suffix=".db")
        os.write(fd, b"synthetic partial copy")
        st = os.fstat(fd)
        os.close(fd)
        return os.path.basename(path), {"dev": st.st_dev, "ino": st.st_ino, "uid": st.st_uid}


@pytest.fixture(params=["update", "recover"])
def world(request, tmp_path, monkeypatch):
    return World(request.param, tmp_path, monkeypatch)


def test_success_records_then_clears_and_leaves_no_copy(world, monkeypatch):
    world.journal()
    seen = []
    real = world.mod.shutil.copyfileobj

    def copy(src, dst, *a):
        seen.append(world.state()["applying"].get("restore_staging"))
        return real(src, dst, *a)
    monkeypatch.setattr(world.mod.shutil, "copyfileobj", copy)
    world.replace()
    record = seen[0]
    assert set(record) == {"name", "dev", "ino", "uid"}          # bound before copying
    assert record["name"].startswith(".restore.") and "/" not in record["name"]
    assert _rows(world.live) == ["backup"]
    assert world.staging() == []
    assert "restore_staging" not in world.state()["applying"]
    assert world.state()["applying"]["command_id"] == "cid-1"    # journal untouched otherwise


@pytest.mark.parametrize("journal", ["missing", "no_applying", "corrupt"])
def test_no_copy_without_a_usable_journal(world, journal):
    if journal == "no_applying":
        world.journal(applying=False)
    elif journal == "corrupt":
        (world.data / "update_state.json").write_text("{not json")
    with pytest.raises(OSError, match="restore_staging_"):
        world.replace()
    assert world.staging() == [] and _rows(world.live) == ["live"]


def test_record_write_failure_removes_only_our_new_copy(world, monkeypatch):
    world.journal()
    name = "save_state" if world.which == "update" else "_save_state"

    def fail(*a, **k):
        raise OSError("synthetic journal write failure")
    monkeypatch.setattr(world.mod, name, fail)
    with pytest.raises(OSError):
        world.replace()
    assert world.staging() == [] and _rows(world.live) == ["live"]


def test_recorded_leftover_of_a_killed_restore_is_cleaned_then_restored(world):
    name, identity = world.leftover()
    world.journal(restore_staging={"name": name, **identity})
    world.replace()
    assert name not in world.staging() and world.staging() == []
    assert _rows(world.live) == ["backup"]
    assert "restore_staging" not in world.state()["applying"]


def test_record_whose_copy_was_already_published_is_simply_cleared(world):
    world.journal(restore_staging={"name": ".restore.abcdefgh.db", "dev": 1, "ino": 2,
                                   "uid": os.getuid()})
    world.replace()
    assert _rows(world.live) == ["backup"] and world.staging() == []


def _mismatch(world, kind):
    name, identity = world.leftover()
    path = world.data / name
    if kind == "inode":
        identity = dict(identity, ino=identity["ino"] + 1)
    elif kind == "owner":
        identity = dict(identity, uid=identity["uid"] + 1)
    elif kind == "symlink":
        path.unlink()
        os.symlink(world.backup, path)
    elif kind == "hardlink":
        os.link(path, world.data / "synthetic-other-link")
    return name, identity


@pytest.mark.parametrize("kind", ["inode", "owner", "symlink", "hardlink"])
def test_unprovable_recorded_file_is_kept_and_restore_is_held(world, kind):
    name, identity = _mismatch(world, kind)
    world.journal(restore_staging={"name": name, **identity})
    with pytest.raises(OSError, match="restore_staging_identity_changed"):
        world.replace()
    assert os.path.lexists(world.data / name)
    assert world.state()["applying"]["restore_staging"]["name"] == name
    assert _rows(world.live) == ["live"]


@pytest.mark.parametrize("record", [
    {"name": "../ledger.db", "dev": 1, "ino": 2, "uid": 0},
    {"name": "/etc/passwd", "dev": 1, "ino": 2, "uid": 0},
    {"name": "ledger.db", "dev": 1, "ino": 2, "uid": 0},
    {"name": ".restore.abcdefgh.db", "dev": "1", "ino": 2, "uid": 0},
    {"name": ".restore.abcdefgh.db", "dev": 1, "ino": 2},
    "synthetic",
])
def test_invalid_record_touches_nothing(world, record):
    world.journal(restore_staging=record)
    with pytest.raises(OSError, match="restore_staging_record_invalid"):
        world.replace()
    assert _rows(world.live) == ["live"] and world.staging() == []


def test_unrecorded_matching_name_is_never_touched(world):
    name, _ = world.leftover()
    world.journal()
    world.replace()
    assert world.staging() == [name]                   # old ambiguous file is kept
    assert _rows(world.live) == ["backup"]


def test_failed_verification_cleans_only_its_own_copy_and_keeps_the_error(world, monkeypatch):
    world.journal()
    with open(world.backup, "ab") as handle:
        handle.write(b"x")                              # sha no longer matches the receipt
    with pytest.raises(OSError, match="backup_changed_during_restore"):
        world.mod._replace_database(str(world.backup), "0" * 64, lambda: None)
    assert world.staging() == [] and _rows(world.live) == ["live"]
    assert "restore_staging" not in world.state()["applying"]


def test_copy_swapped_before_cleanup_is_kept_with_its_record(world, monkeypatch):
    world.journal()
    real = world.mod._file_sha256

    def swap(path):
        os.unlink(path)
        with open(path, "wb") as handle:                # a different inode now
            handle.write(b"synthetic foreign file")
        return real(path)
    monkeypatch.setattr(world.mod, "_file_sha256", swap)
    with pytest.raises(OSError, match="backup_changed_during_restore"):
        world.replace()
    assert len(world.staging()) == 1
    assert "restore_staging" in world.state()["applying"]
    assert _rows(world.live) == ["live"]
