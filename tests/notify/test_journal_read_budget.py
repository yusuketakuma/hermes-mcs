"""Optional operator read limits never turn a partial journal into evidence."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

import notify_journal
import notify_spool


def _journals(tmp_path):
    notify_spool.ensure_dirs(str(tmp_path))
    dirs = notify_spool.notify_dirs(str(tmp_path))
    raw = json.dumps({"phase": "started", "attempt_id": "synthetic"}).encode() + b"\n"
    for transport in ("slack", "discord"):
        Path(dirs[transport + "_state"], "journal-synthetic.jsonl").write_bytes(raw)
    return dirs, raw


def test_unlimited_read_and_exact_limits_agree(tmp_path):
    dirs, raw = _journals(tmp_path)
    expected = notify_journal.scan_journals(dirs)
    assert notify_journal.scan_journals(dirs, max_bytes=2 * len(raw), max_rows=2) == expected
    assert not expected[1] and len(expected[0]["synthetic"]["rows"]) == 2


@pytest.mark.parametrize("budget", ["bytes", "rows"])
def test_limits_apply_across_files_and_discard_the_partial_observation(tmp_path, budget):
    dirs, raw = _journals(tmp_path)
    limit = {"max_bytes": len(raw)} if budget == "bytes" else {"max_rows": 1}
    with pytest.raises(notify_journal.JournalReadLimit, match="^journal_read_limit$"):
        notify_journal.scan_journals(dirs, **limit)
    assert not notify_journal.scan_journals(dirs)[1]


def test_oversized_line_is_not_read_without_a_byte_bound(tmp_path, monkeypatch):
    dirs, _raw = _journals(tmp_path)
    sizes = []

    class Oversized:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def readline(self, size):
            sizes.append(size)
            return b"x" * size

    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: Oversized())
    with pytest.raises(notify_journal.JournalReadLimit):
        notify_journal.scan_journals(dirs, max_bytes=100)
    assert sizes == [101]


def test_expired_deadline_stops_before_filesystem_access(monkeypatch):
    monkeypatch.setattr(notify_journal.time, "monotonic", lambda: 10)
    monkeypatch.setattr(notify_journal.os, "listdir", lambda *args: pytest.fail("read after deadline"))
    dirs = {transport + "_state": "synthetic" for transport in notify_journal.SUPPORTED_TRANSPORTS}
    with pytest.raises(notify_journal.JournalReadLimit):
        notify_journal.scan_journals(dirs, deadline=10)


@pytest.mark.parametrize("limit", [True, 0, -1, 1.5, "1"])
@pytest.mark.parametrize("key", ["max_bytes", "max_rows"])
def test_invalid_limits_are_rejected_before_reading(key, limit):
    with pytest.raises(ValueError, match="^bad_journal_budget$"):
        notify_journal.scan_journals({}, **{key: limit})


@pytest.mark.parametrize("deadline", [True, "1", float("nan"), float("inf")])
def test_invalid_deadline_is_rejected_before_reading(deadline):
    with pytest.raises(ValueError, match="^bad_journal_budget$"):
        notify_journal.scan_journals({}, deadline=deadline)


@pytest.mark.parametrize("duplicate", [False, True])
def test_evidence_module_import_preserves_the_core_only_bootstrap(tmp_path, duplicate):
    root = Path(__file__).resolve().parents[2]
    code = r'''import sys,builtins,json
from pathlib import Path
def offline(event,args):
    if event in ("socket.connect","socket.getaddrinfo","subprocess.Popen","os.system"):
        raise AssertionError("external_effect_forbidden")
sys.addaudithook(offline)
sys.path.insert(0,sys.argv[1])
import _mcs_path
original=builtins.__import__
def checked(name,*args,**kwargs):
    if name == "adapters" or name.startswith("adapters."):
        raise ImportError("adapter_not_in_candidate_bootstrap")
    return original(name,*args,**kwargs)
builtins.__import__=checked
import notify_journal,notify_spool,notify_reconcile
from ledger import Ledger
assert "started" in notify_journal.PHASES
base=Path(sys.argv[2])
notify_spool.ensure_dirs(str(base))
dirs=notify_spool.notify_dirs(str(base))
witness=Path(dirs["slack_state"])/"journal-synthetic.jsonl"
row={"attempt_id":"synthetic-attempt","delivery_id":"123e4567-e89b-42d3-a456-426614174000","phase":"begin"}
raw=(json.dumps(row)+"\n").encode()
if sys.argv[3] == "1":
    raw+=b'{"attempt_id":"synthetic-attempt","delivery_id":"123e4567-e89b-42d3-a456-426614174000","phase":"started","phase":"begin"}\n'
else:
    raw+=(json.dumps({**row,"phase":"started"})+"\n").encode()
witness.write_bytes(raw)
attempts,incomplete=notify_journal.scan_journals(dirs)
assert incomplete == (sys.argv[3] == "1")
assert attempts["synthetic-attempt"]["tainted"] == incomplete
led=Ledger(str(base/"ledger.db"))
try:
    notify_spool.mark_restored(str(base),by="synthetic-test",now=1000)
    report=notify_reconcile.reconcile_after_restore(led,{},now=1001)
    assert report["journal_incomplete"] == incomplete
    assert report["counts"].get("not_sent",0) == 0
    assert report["counts"]["held"] == 1
    assert bool(notify_spool.restore_pending(str(base))) == incomplete
    assert led.db.execute("SELECT COUNT(*) FROM notification_restore_holds").fetchone()[0] == 1
    assert witness.read_bytes() == raw
finally:
    led.close()
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(root / "mcs"),
                             str(tmp_path / "data"), str(int(duplicate))],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
