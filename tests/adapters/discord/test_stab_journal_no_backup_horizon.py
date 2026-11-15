"""Regression: journal compaction falls back to a time horizon without a backup set."""
import asyncio
import os
import time

import backup_names
from adapters.common import journal
from discord_delivery_testkit import _mkworker, _settled, _state

DONE = "00000000-0000-4000-8000-0000000000d1"
UNKNOWN = "00000000-0000-4000-8000-0000000000d2"
RECENT = "00000000-0000-4000-8000-0000000000d3"


def _seed(tmp_path):
    w, reg, _ = _mkworker(tmp_path)
    state = _state(tmp_path)
    now = time.time()
    ancient = now - backup_names.JOURNAL_HORIZON_MAX_S - 2 * 86400
    _settled(state, "w0", "c1" * 8, DONE, ancient)
    _settled(state, "w0", "c2" * 8, UNKNOWN, ancient, result="unknown")
    _settled(state, "w0", "c3" * 8, RECENT, now - 30 * 86400)
    for did in (DONE, UNKNOWN, RECENT):
        reg.put_parts_done(did)
    return w, state


def test_no_backup_directory_prunes_only_settled_rows_past_the_long_horizon(tmp_path):
    w, state = _seed(tmp_path)
    assert not (tmp_path / "backups").exists()
    before = journal.scan(str(state))
    asyncio.run(w.maintain_journal(rotate=True))
    after = journal.scan(str(state))
    assert set(before) - set(after) == {"c1" * 8}
    assert after["c2" * 8] == before["c2" * 8]      # unknown outcome is kept
    assert after["c3" * 8] == before["c3" * 8]      # inside the horizon


def test_empty_backup_directory_uses_the_same_fallback(tmp_path):
    w, state = _seed(tmp_path)
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "notes.txt").write_text("synthetic")
    asyncio.run(w.maintain_journal(rotate=True))
    assert "c1" * 8 not in journal.scan(str(state))


def test_unreadable_backup_directory_still_prunes_nothing(tmp_path):
    w, state = _seed(tmp_path)
    (tmp_path / "backups").write_text("not a directory")
    before = journal.scan(str(state))
    asyncio.run(w.maintain_journal(rotate=True))
    assert journal.scan(str(state)) == before


def test_restore_pending_still_pauses_fallback_pruning(tmp_path):
    w, state = _seed(tmp_path)
    with open(os.path.join(w._root, "restore_pending.json"), "w") as stream:
        stream.write("{}")
    before = journal.scan(str(state))
    asyncio.run(w.maintain_journal(rotate=True))
    assert journal.scan(str(state)) == before


def test_an_owner_kept_ancient_copy_no_longer_stops_compaction(tmp_path):
    """A reset or hand-made copy older than the cap used to pin the horizon
    to its mtime, so nothing was ever pruned again."""
    w, state = _seed(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    old = time.time() - 400 * 86400
    for name in ("patient-context-reset-20250101-000000-x.db", "hand-copy.db"):
        (backups / name).write_bytes(b"synthetic")
        os.utime(backups / name, (old, old))
    asyncio.run(w.maintain_journal(rotate=True))
    after = journal.scan(str(state))
    assert "c1" * 8 not in after and "c3" * 8 in after


def test_a_recent_backup_still_keeps_its_rows(tmp_path):
    w, state = _seed(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    daily = backups / "ledger-20260101.db"
    daily.write_bytes(b"synthetic")
    newer = time.time() - 10 * 86400
    os.utime(daily, (newer, newer))
    staged = backups / "ledger-20251001-x.db.tmp"   # staging never dates a restore
    staged.write_bytes(b"synthetic")
    os.utime(staged, (0, 0))
    asyncio.run(w.maintain_journal(rotate=True))
    after = journal.scan(str(state))
    assert "c1" * 8 not in after and "c3" * 8 not in after
    assert "c2" * 8 in after
