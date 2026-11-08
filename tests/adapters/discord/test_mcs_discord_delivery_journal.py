"""Synthetic mcs discord delivery journal contracts with isolated shared fixtures."""
from __future__ import annotations
import asyncio
import json
import sys
import pytest
from adapters.common import envelopes, journal, paths
from discord_delivery_testkit import _chunks, _claim, _envelope, _mkworker, _sent_parts, _settled, _spec, _state
from discord_delivery_testkit import synthetic_discord_sdk as synthetic_discord_sdk
from mcs_discord_delivery_testkit import _backup as _backup, _count_journal_reads as _count_journal_reads, _did as _did, _publish_spec as _publish_spec, _same_as_full_scan as _same_as_full_scan, _sealed as _sealed


def test_new_journal_segment_is_owner_only(tmp_path):
    import os
    import stat
    old = os.umask(0o022)
    try:
        path = journal.append(str(tmp_path), "w0",
                              {"phase": "claimed", "attempt_id": "a1"})
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    journal.append(str(tmp_path), "w0",
                   {"phase": "begin", "attempt_id": "a1"})
    assert len(journal.scan(str(tmp_path))["a1"]) == 2


@pytest.mark.parametrize("damaged", [False, True])
def test_journal_compaction_drops_only_settled_pre_backup_attempts(
        tmp_path, damaged):
    import time as _time
    w, reg, _ = _mkworker(tmp_path)
    state = _state(tmp_path)
    now = _time.time()
    old, recent = now - 5 * 86400, now - 3600
    ids = {k: f"00000000-0000-4000-8000-0000000000{i:02d}"
           for i, k in enumerate(("done", "unknown", "open", "recent",
                                  "claimed", "pending", "foreign"))}
    _settled(state, "w0", "a1" * 8, ids["done"], old)
    _settled(state, "w0", "a2" * 8, ids["unknown"], old, result="unknown")
    journal.append(str(state), "w0", {
        "phase": "started", "attempt_id": "a3" * 8,
        "delivery_id": ids["open"], "ts": old,
        "receipt_envelope": _envelope()})
    _settled(state, "w0", "a4" * 8, ids["recent"], recent)
    _settled(state, "w0", "a5" * 8, ids["claimed"], old)
    _settled(state, "w0", "a6" * 8, ids["pending"], old)
    _settled(state, "w9", "a7" * 8, ids["foreign"], old, channel_id="99")
    if damaged:
        _settled(state, "w8", "a8" * 8, ids["done"] + "x", old)
        with (state / "journal-w8.jsonl").open("a") as stream:
            stream.write("{torn\n")
    reg.put_parts_done(ids["done"])
    reg.claim(ids["claimed"], {"attempt_id": "zz", "phase": "settled"})
    pending = dict(_sealed(_spec(_chunks(1))), delivery_id=ids["pending"])
    _publish_spec(tmp_path, pending)             # parts not yet done
    before = journal.scan(str(state))
    before_bytes = {p.name: p.read_bytes()
                    for p in state.glob("journal-*.jsonl")}

    # no backup yet: the 90-day fallback horizon keeps these 5-day-old
    # rows, so nothing is pruned regardless of file damage
    asyncio.run(w.maintain_journal(rotate=True))
    assert journal.scan(str(state)) == before

    _backup(tmp_path, now - 86400)
    asyncio.run(w.maintain_journal(rotate=True))
    after = journal.scan(str(state))
    if damaged:
        # an unreadable row can hide another file's attempt membership:
        # the whole pass drops nothing and every file stays byte-for-byte
        assert after == before
        assert {p.name: p.read_bytes()
                for p in state.glob("journal-*.jsonl")} == before_bytes
        assert (state / "journal-w8.jsonl").read_text().endswith("{torn\n")
    else:
        assert "a1" * 8 not in after
        assert set(before) - set(after) == {"a1" * 8}
        assert after["a3" * 8] == before["a3" * 8]
    # the recorded crash-recovery classification is unchanged
    assert journal.unfinished(after).keys() == journal.unfinished(before).keys()
    assert journal.unreported(after).keys() == journal.unreported(before).keys()


def test_journal_compaction_pauses_while_a_restore_is_pending(tmp_path):
    """The journal is the post-restore reconcile's evidence; a pending
    restore may retire its backup and move the horizon — never prune."""
    import time as _time
    w, _reg, _ = _mkworker(tmp_path)
    state = _state(tmp_path)
    now = _time.time()
    _settled(state, "w0", "a1" * 8,
             "00000000-0000-4000-8000-000000000001", now - 5 * 86400)
    _backup(tmp_path, now - 86400)
    from pathlib import Path
    (Path(w._root) / "restore_pending.json").write_text("{}")
    before = journal.scan(str(state))
    asyncio.run(w.maintain_journal(rotate=True))
    assert journal.scan(str(state)) == before


@pytest.mark.parametrize("seed", range(20))
def test_scan_cache_equals_full_scan_under_random_journal_mutation(
        tmp_path, seed):
    """Appends, torn/corrupt lines, compact-style atomic rewrites,
    unlink + same-name recreate and truncation — the cache, refreshed
    at random points, always returns exactly what a full scan does."""
    import os
    import random
    rnd = random.Random(seed)
    cache = journal.ScanCache(str(tmp_path))
    held = []                 # (view, full scan when it was taken)
    names = [f"journal-w{i}.jsonl" for i in range(3)]
    aids = [f"a{i}" for i in range(6)]
    for _ in range(80):
        path = tmp_path / rnd.choice(names)
        op = rnd.randrange(8)
        if op <= 2:
            journal.append(str(tmp_path), path.name[8:-6], {
                "phase": rnd.choice(journal.PHASES),
                "attempt_id": rnd.choice(aids), "n": rnd.random()})
        elif op == 3:                     # torn tail, maybe parseable
            with path.open("ab") as f:
                f.write(rnd.choice([b'{"attempt_id":"a1"',
                                    b'{"attempt_id":"a2","phase":"result"}',
                                    b"\n", b"garbage\n", b"[1]\n"]))
        elif op == 4 and path.exists():   # compact-style atomic rewrite
            lines = path.read_bytes().split(b"\n")
            keep = [ln for ln in lines if rnd.random() < 0.6]
            paths.atomic_write(str(path), b"\n".join(keep),
                               tmp_prefix=".journal-", mode=0o600)
        elif op == 5 and path.exists():   # same name, new longer file
            data = path.read_bytes()
            path.unlink()
            path.write_bytes(data[: len(data) // 2] + b"\n" + data)
        elif op == 6 and path.exists():
            with path.open("r+b") as f:
                f.truncate(rnd.randrange(os.path.getsize(path) + 1))
        elif op == 7 and path.exists():
            path.unlink()
        if rnd.random() < 0.5:
            view = cache.refresh()
            _same_as_full_scan(view, tmp_path)
            held.append((view, journal.scan(str(tmp_path))))
    _same_as_full_scan(cache.refresh(), tmp_path)
    # a view held across later refreshes stays the scan it was taken as
    for view, full in held:
        assert list(view) == list(full)
        assert {aid: view[aid] for aid in view} == full


def test_scan_cache_rereads_a_replaced_file_and_falls_back_on_oserror(
        tmp_path):
    """A same-length rewrite that keeps the last line in place is only
    visible as a new inode; an unreadable file drops to a full scan."""
    import os
    cache = journal.ScanCache(str(tmp_path))
    path = tmp_path / "journal-w0.jsonl"
    path.write_bytes(b'{"attempt_id":"a1"}\n{"attempt_id":"zz"}\n')
    _same_as_full_scan(cache.refresh(), tmp_path)
    paths.atomic_write(str(path),
                       b'{"attempt_id":"b2"}\n{"attempt_id":"zz"}\n'
                       b'{"attempt_id":"c3"}\n', tmp_prefix=".journal-")
    _same_as_full_scan(cache.refresh(), tmp_path)
    other = tmp_path / "journal-w1.jsonl"
    other.write_bytes(b'{"attempt_id":"d4"}\n')
    os.chmod(other, 0)
    try:
        _same_as_full_scan(cache.refresh(), tmp_path)
    finally:
        os.chmod(other, 0o600)
    _same_as_full_scan(cache.refresh(), tmp_path)


def test_tick_journal_reads_match_full_scan_across_rotation(
        tmp_path, monkeypatch):
    """_spent_deliveries, _started, _resume_dead and the _resume_parts
    fallback read the incremental view; at each call it equals a fresh
    full scan and they decide exactly as a full scan would — across a
    peer's appends, a torn tail, compaction behind the cache's back,
    in-worker rotation + compaction and a deleted segment."""
    import time as _time
    from adapters.common import worker as worker_mod
    state = _state(tmp_path)
    w, reg, _bot = _mkworker(tmp_path)
    monkeypatch.setattr(worker_mod, "JOURNAL_SEGMENT_BYTES", 2048)
    views = []
    real_refresh = w._jview.refresh

    def refresh():
        view = real_refresh()
        _same_as_full_scan(view, state)
        views.append((view, journal.scan(str(state))))
        return view
    w._jview.refresh = refresh
    scans = []
    real_scan = journal.scan

    def scan(d):
        # only the worker's own full scans count, not this test's checks
        if sys._getframe(1).f_code.co_filename == worker_mod.__file__:
            scans.append(d)
        return real_scan(d)
    monkeypatch.setattr(journal, "scan", scan)

    def full_spent():
        return {str(r.get("delivery_id"))
                for rows in real_scan(str(state)).values() for r in rows
                if r.get("phase") in ("started", "result")
                and not r.get("part_id")}

    def full_started(aid):
        return any(r.get("phase") == "started"
                   for r in real_scan(str(state)).get(aid, []))

    def check(i):
        assert w._spent_deliveries() == full_spent()
        for aid in ("ab" * 8, f"s{i}"):
            assert w._started({"attempt_id": aid}) == full_started(aid)
        spec = dict(_spec(_chunks(2)), delivery_id=_did(i))
        journal.append(str(state), "w0", {
            "phase": "result", "attempt_id": f"card{i}",
            "delivery_id": _did(i), "result": "delivered",
            "message_id": "9001"})
        asyncio.run(w._resume_dead([spec]) if i % 2
                    else w._resume_parts(spec))
        assert reg.parts_done(_did(i))
        assert set(_sent_parts(state, _did(i))) \
            == {"thread", "body:0001", "body:0002"}

    old = _time.time() - 5 * 86400
    _settled(state, "w8", "c1" * 8, _did(90), old)
    _settled(state, "w8", "c2" * 8, _did(91), old)
    check(1)
    journal.append(str(state), "w9", {"phase": "started",
                                      "attempt_id": "s2",
                                      "delivery_id": _did(80)})
    with (state / "journal-w9.jsonl").open("ab") as f:
        f.write(json.dumps({"phase": "started", "attempt_id": "s3",
                            "delivery_id": _did(81)}).encode())
    check(2)                  # torn but parseable tail counts
    with (state / "journal-w9.jsonl").open("ab") as f:
        f.write(b"\n")
    journal.compact(str(state), active="", file_ok=lambda rows: True,
                    prunable=lambda a, rows: a == "c1" * 8)
    check(3)                  # compacted behind the cache's back
    _backup(tmp_path, _time.time() + 3 * 86400)
    for i in (1, 2, 3):
        reg.put_parts_done(_did(i))
    asyncio.run(w.maintain_journal())
    assert w._segment >= 1
    check(4)                  # rotated + compacted in-worker
    (state / "journal-w9.jsonl").unlink()
    check(5)
    assert "c2" * 8 not in real_scan(str(state))
    assert scans == [] and len(views) >= 15
    # every view handed out is still the scan it was taken as — the
    # resume loop holds one while its own sends append rows
    for view, full in views:
        assert {aid: view[aid] for aid in view} == full
        # the flat walk yields exactly the snapshot's rows, once each
        flat = sorted(json.dumps(r, sort_keys=True) for r in view.rows())
        assert flat == sorted(json.dumps(r, sort_keys=True)
                              for rows in full.values() for r in rows)


def test_part_dedupe_reads_only_appended_journal_bytes(tmp_path,
                                                       monkeypatch):
    """Per-card part dedupe costs O(new rows), not O(journal): no full
    scan, and the bytes read over n cards stay within ~2x the journal."""
    state = _state(tmp_path)
    w, _reg, _ = _mkworker(tmp_path)
    scans = []
    real_scan = journal.scan
    monkeypatch.setattr(journal, "scan",
                        lambda d: scans.append(d) or real_scan(d))
    got = _count_journal_reads(monkeypatch)
    per_card = []
    for i in range(1, 41):
        got.clear()
        asyncio.run(w._deliver_parts(
            _claim(dict(_spec(_chunks(3)), delivery_id=_did(i))), "9001"))
        per_card.append(sum(got))
    size = sum(p.stat().st_size for p in state.glob("journal-*.jsonl"))
    assert scans == []
    assert sum(per_card) <= 2 * size
    # constant per card: the 40th card reads no more than the 2nd did
    assert per_card[-1] <= 2 * per_card[1]


def test_tick_journal_reads_do_not_grow_with_the_journal(tmp_path,
                                                         monkeypatch):
    """A tick that claims a fresh spec (spent check), resumes a dead
    spec's parts and settles a withdrawn granted claim (started check)
    reads only the journal bytes appended since the last tick — not the
    whole journal again, however large it has grown."""
    import time as _time
    state = _state(tmp_path)
    w, reg, _ = _mkworker(tmp_path)
    for i in range(300):            # a large settled history
        _settled(state, "w8", f"h{i:07d}", _did(10_000 + i), _time.time())
    size = sum(p.stat().st_size for p in state.glob("journal-*.jsonl"))
    got = _count_journal_reads(monkeypatch)
    per_tick = []
    for i in range(1, 13):
        _publish_spec(tmp_path, _sealed(dict(_spec(_chunks(1)),
                                             delivery_id=_did(i))))
        dead = dict(_spec(_chunks(1)), delivery_id=_did(500 + i))
        _publish_spec(tmp_path, _sealed(dead))
        reg.mark_dead(dead["delivery_id"])
        journal.append(str(state), "w0", {
            "phase": "result", "attempt_id": f"card{i}",
            "delivery_id": dead["delivery_id"], "result": "delivered",
            "message_id": "9001"})
        gone = dict(_spec(_chunks(1)), delivery_id=_did(900 + i))
        reg.claim(gone["delivery_id"], {
            "attempt_id": f"g{i}", "worker_id": "w1", "spec": gone,
            "payload_hash": envelopes.payload_hash(gone),
            "spec_path": str(tmp_path / "gone.json"), "phase": "granted"})
        got.clear()
        asyncio.run(w.tick())
        per_tick.append(sum(got))
        assert reg.claimed(_did(i)) and reg.parts_done(dead["delivery_id"])
        assert not reg.claimed(gone["delivery_id"])
    # the first tick reads the history once; later ticks only new rows
    assert per_tick[0] >= size
    assert max(per_tick[1:]) < size / 10
    assert per_tick[-1] <= 2 * per_tick[1]
