"""Rule CLI resources close on failures using isolated SQLite and owned descriptors."""
import errno
import os
import sqlite3
import sys

import pytest

import extract
from ledger import Ledger


@pytest.mark.parametrize("stats", [False, True])
@pytest.mark.parametrize("failure", ["constructor", "query", "extract", "json", "stats", "close"])
def test_rule_cli_releases_owned_connection_and_lock_on_failure(
        tmp_path, monkeypatch, capsys, stats, failure):
    ledger = None
    close = None
    keeper = None
    owned = None
    try:
        ledger = Ledger(str(tmp_path / "synthetic.db"))
        close = ledger.close
        ledger.artifact_add(extract.KIND, '{"events":["visit"]}')
        lock_path = tmp_path / "synthetic.lock"
        keeper = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        owned = os.dup(keeper)
        acquired, closed = [], []
        error = ValueError("synthetic-" + failure)
        monkeypatch.setattr(sys, "argv", ["extract", *( ["--stats"] if stats else [])])

        def acquire():
            acquired.append(True)
            return owned

        def construct(path):
            assert path == "synthetic-not-opened.db"
            if failure == "constructor":
                raise error
            return ledger

        def release():
            closed.append(True)
            close()
            if failure == "close":
                raise error

        def fail(*args, **kwargs):
            raise error

        monkeypatch.setattr(extract, "DB", "synthetic-not-opened.db")
        monkeypatch.setattr(extract, "acquire_run_lock", acquire)
        monkeypatch.setattr(extract, "LedgerReader", construct)
        monkeypatch.setattr(extract, "Ledger", construct)
        monkeypatch.setattr(ledger, "close", release)
        if failure == "query":
            ledger.db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY
                                     if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK)
        elif failure == "extract":
            monkeypatch.setattr(extract, "_extract_rows", fail)
        elif failure == "json":
            monkeypatch.setattr(extract.json, "dumps", fail)
        elif failure == "stats":
            monkeypatch.setattr(extract, "loads_dict", fail)
        with pytest.raises(sqlite3.DatabaseError if failure == "query" else ValueError) as caught:
            extract.main()
        if failure != "query":
            assert caught.value is error
        assert acquired == ([] if stats else [True])
        assert closed == ([] if failure == "constructor" else [True])
        if failure != "constructor":
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                ledger.db.execute("SELECT 1")
        if not stats:
            with pytest.raises(OSError) as fd_error:
                os.fstat(owned)
            assert fd_error.value.errno == errno.EBADF
        assert os.fstat(keeper).st_size == 0
        output = capsys.readouterr().out
        if failure in ("constructor", "query", "extract", "json"):
            assert output == ""
        elif failure == "stats":
            assert '"extracted": 0' in output and '"field_coverage"' not in output
        else:
            assert '"extracted": 0' in output and '"field_coverage"' in output
    finally:
        try:
            if close is not None:
                close()
        finally:
            try:
                if owned is not None:
                    try:
                        os.close(owned)
                    except OSError as exc:
                        if exc.errno != errno.EBADF:
                            raise
            finally:
                if keeper is not None:
                    try:
                        os.close(keeper)
                    except OSError as exc:
                        if exc.errno != errno.EBADF:
                            raise
