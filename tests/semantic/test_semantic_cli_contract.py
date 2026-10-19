"""Offline/status entry points cannot implicitly start a model drain."""
import pytest
import semantic
from semantic_testkit import _seeded


def test_offline_status_is_read_only_and_drain_requires_explicit_action(tmp_path, monkeypatch, capsys):
    db = _seeded(tmp_path)
    path = tmp_path / "ledger.db"
    db.close()
    monkeypatch.setattr(semantic, "DB", str(path))
    before = path.read_bytes()
    monkeypatch.setattr(semantic.sys, "argv", ["semantic", "--status", "--offline"])
    assert semantic.main() == 0
    assert path.read_bytes() == before
    for flags in ([], ["--drain", "--offline"], ["--drain", "--max-jobs", "-1"],
                  ["--replay", "0", "--offline"], ["--replay", "-1"],
                  ["--replay", str(2**63)]):
        monkeypatch.setattr(semantic.sys, "argv", ["semantic", *flags])
        with pytest.raises(SystemExit) as error:
            semantic.main()
        assert error.value.code == 2
    assert path.read_bytes() == before
    capsys.readouterr()


@pytest.mark.parametrize("stage", ["configuration", "report", "encoding"])
def test_status_closes_its_reader_when_read_or_output_preparation_fails(
        tmp_path, monkeypatch, capsys, stage):
    import sqlite3

    ledger = _seeded(tmp_path)
    path = tmp_path / "ledger.db"
    ledger.close()
    before = path.read_bytes()
    opened, closed = [], []
    factory = semantic.LedgerReader
    encode = semantic.json.dumps

    def tracked_reader(database):
        reader = factory(database)
        close = reader.close
        def counted_close():
            closed.append(reader)
            close()
        reader.close = counted_close
        opened.append((reader, close))
        return reader

    def fail(*args, **kwargs):
        raise ValueError("synthetic status preparation")

    monkeypatch.setattr(semantic, "LedgerReader", tracked_reader)
    monkeypatch.setattr(semantic, "DB", str(path))
    monkeypatch.setattr(semantic.sys, "argv", ["semantic", "--status", "--offline"])
    monkeypatch.setattr(semantic, "load_config", fail if stage == "configuration" else lambda _: {})
    monkeypatch.setattr(semantic, "status_report", fail if stage == "report"
                        else lambda *args: {"synthetic_status": True})
    if stage == "encoding":
        def fail_status_encoding(value, **kwargs):
            if value == {"synthetic_status": True}:
                fail()
            return encode(value, **kwargs)
        monkeypatch.setattr(semantic.json, "dumps", fail_status_encoding)
    try:
        with pytest.raises(ValueError, match="synthetic status preparation"):
            semantic.main()
        assert len(opened) == 1 and closed == [opened[0][0]]
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0][0].db.execute("SELECT 1")
        assert path.read_bytes() == before
        assert capsys.readouterr().out == ""
    finally:
        for reader, original_close in opened:
            original_close()


@pytest.mark.parametrize("fault", ["close", "seed_and_close", "configuration_and_close"])
def test_writer_lock_is_released_even_when_writer_close_raises(
        tmp_path, monkeypatch, capsys, fault):
    import errno
    import json
    import os
    import tempfile

    opened, closed = [], []
    with tempfile.TemporaryFile(dir=tmp_path) as backing:
        owned_fd = os.dup(backing.fileno())
        path = str(tmp_path / "synthetic-never-opened.db")

        class Writer:
            def __init__(self, database):
                opened.append(database)
            def close(self):
                closed.append(True)
                raise OSError("synthetic writer close")

        def seed(writer, mid):
            assert type(writer) is Writer and mid == 1
            if fault == "seed_and_close":
                raise ValueError("synthetic seed")
            return 1

        def config(_):
            raise ValueError("synthetic configuration")

        monkeypatch.setattr(semantic, "DB", path)
        monkeypatch.setattr(semantic, "Ledger", Writer)
        monkeypatch.setattr(semantic, "acquire_run_lock", lambda: owned_fd)
        monkeypatch.setattr(semantic, "seed", seed)
        monkeypatch.setattr(semantic, "load_config", config)
        monkeypatch.setattr(semantic.semantic_drain, "run_due", lambda *a, **k: {"done": 0})
        flags = ["--drain"] if fault == "configuration_and_close" else ["--replay", "1"]
        monkeypatch.setattr(semantic.sys, "argv", ["semantic", *flags])
        try:
            with pytest.raises(OSError, match="synthetic writer close") as failed:
                semantic.main()
            assert opened == [path] and closed == [True]
            with pytest.raises(OSError) as released:
                os.fstat(owned_fd)
            assert released.value.errno == errno.EBADF
            # A sibling borrowed descriptor remains owned by its original stream.
            assert os.fstat(backing.fileno()).st_ino >= 0
            assert not (tmp_path / "synthetic-never-opened.db").exists()
            output = capsys.readouterr()
            assert output.err == ""
            if fault == "close":
                assert json.loads(output.out) == {"ok": True, "root": 1}
                assert failed.value.__context__ is None
            else:
                assert output.out == ""
                assert type(failed.value.__context__) is ValueError
        finally:
            try:
                os.close(owned_fd)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
