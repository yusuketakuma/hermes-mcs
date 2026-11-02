"""Extraction retries a result whose per-write lock wait timed out,
carries it across resident run_pending calls, and the --lane CLI maps
lanes to wire slots."""
import writer_lock
import json
import os
import time

import pytest

import extract_llm
import cloud_inference
from extract_testkit import _ledger, _message


def test_batch_default_off():
    assert extract_llm._BATCH_K == 0


def _lock_stub(monkeypatch, tmp_path, blocked_after_call):
    """acquire_run_lock stub: free, until the LLM call starts — then the
    tick holds it for `blocked_after_call` attempts."""
    state = {"blocked": 0}   # returned so a test can free/block it later

    def acquire():
        if state["blocked"] > 0:
            state["blocked"] -= 1
            return None
        return os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR)

    def extract(body, **_kw):
        state["blocked"] = blocked_after_call
        return {"summary": "合成", "vitals": {"bt": 37.0},
                "symptoms": [{"text": "咳"}]}

    monkeypatch.setattr(extract_llm, "acquire_run_lock", acquire)
    monkeypatch.setattr(extract_llm, "llm_extract", extract)
    monkeypatch.setattr(extract_llm.time, "sleep", lambda s: None)
    return state


# run_pending defaults to carry=None, so the two lock_lost tests keep
# their pre-carry semantics: deferred at the end of the run, never carried.
def test_lock_lost_result_is_written_later_in_run(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="合成の本文です。" * 20)])
    # the in-handler wait (60 tries) all fail; the end-of-run retry wins
    _lock_stub(monkeypatch, tmp_path, blocked_after_call=60)
    res = extract_llm.run_pending(db, limit=10, budget_s=30,
                                  per_write_lock=True)
    assert res["lock_lost"] == 1 and res["done"] == 1
    assert res["deferred"] == 0 and res["failed"] == 0
    assert len(db.artifacts("extract_llm", message_id=1)) == 1
    db.close()


def test_lock_lost_twice_defers_without_burning_attempts(tmp_path,
                                                         monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="合成の本文です。" * 20)])
    _lock_stub(monkeypatch, tmp_path, blocked_after_call=10 ** 6)
    res = extract_llm.run_pending(db, limit=10, budget_s=30,
                                  per_write_lock=True)
    assert res["lock_lost"] == 1 and res["deferred"] == 1
    assert res["done"] == 0 and res["failed"] == 0
    # no error row — the message simply stays pending
    assert db.artifacts("extract_llm", message_id=1) == []
    assert res["left"] == 1
    assert res["carried"] == 0 and res["carry"] == []
    db.close()


def _claims(db):
    return [row[0] for row in db.db.execute(
        "SELECT next_try FROM fetch_jobs WHERE kind='extract_claim'")]


def _carry_one(db, monkeypatch, tmp_path):
    """First resident call: the lock stays held, so the finished result
    rides out in res["carry"] with its claim lease intact."""
    db.save_messages([_message(body="合成の本文です。" * 20)])
    state = _lock_stub(monkeypatch, tmp_path, blocked_after_call=10 ** 6)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, per_write_lock=True,
                                  write_wait_s=1, carry=[])
    assert len(res["carry"]) == 1 and res["lock_lost"] == 1
    assert res["deferred"] == 0 and res["done"] == 0 and res["carried"] == 0
    assert len(_claims(db)) == 1
    assert db.artifacts("extract_llm", message_id=1) == []
    return state, res["carry"]


def test_lock_bound_result_is_carried_and_committed_next_call(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    state["blocked"] = 0
    res = extract_llm.run_pending(db, limit=10, budget_s=30, per_write_lock=True,
                                  write_wait_s=1, carry=carry)
    assert res["carried"] == 1 and res["carry"] == [] and res["deferred"] == 0
    assert res["done"] == 1
    assert len(db.artifacts("extract_llm", message_id=1)) == 1
    db.close()


def test_carried_commit_releases_its_claim(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    state["blocked"] = 0
    extract_llm.run_pending(db, limit=10, budget_s=30, per_write_lock=True,
                            write_wait_s=1, carry=carry)
    assert _claims(db) == []
    db.close()


def test_carried_result_near_lease_expiry_is_dropped(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    monkeypatch.setattr(extract_llm, "_CARRY_MARGIN_S", 10 ** 6)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, per_write_lock=True,
                                  write_wait_s=1, carry=carry)
    assert res["deferred"] == 1 and res["carried"] == 0 and res["carry"] == []
    assert db.artifacts("extract_llm", message_id=1) == []
    assert _claims(db) == []
    assert res["left"] == 1
    db.close()


def _decay_lease(db, carry, left_s):
    """Context units ate most of the claim lease: the row and the carried
    tuple now expire `left_s` from now."""
    r, _lease, commit = carry[0]
    decayed = time.time() + left_s
    db.db.execute("UPDATE fetch_jobs SET next_try=? WHERE kind='extract_claim'",
                  (decayed,))
    db.db.commit()
    carry[0] = (r, decayed, commit)


def test_carried_result_lease_is_renewed_while_lock_bound(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    _decay_lease(db, carry, extract_llm._CARRY_MARGIN_S + 80)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, per_write_lock=True,
                                  write_wait_s=1, carry=carry)
    assert res["deferred"] == 0 and len(res["carry"]) == 1
    renewed = res["carry"][0][1]
    # outlasts one 900 s context unit plus the carry margin
    assert renewed - time.time() > 900 + extract_llm._CARRY_MARGIN_S
    assert _claims(db) == [renewed]
    state["blocked"] = 0
    res = extract_llm.run_pending(db, limit=10, budget_s=30, per_write_lock=True,
                                  write_wait_s=1, carry=res["carry"])
    assert res["carried"] == 1 and res["carry"] == []
    assert len(db.artifacts("extract_llm", message_id=1)) == 1
    assert _claims(db) == []
    db.close()


def test_carried_result_with_lost_lease_is_dropped(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    # another worker re-claimed the row after our lease lapsed
    db.db.execute("UPDATE fetch_jobs SET next_try=next_try+1 WHERE kind='extract_claim'")
    db.db.commit()
    res = extract_llm.run_pending(db, limit=10, budget_s=30, per_write_lock=True,
                                  write_wait_s=1, carry=carry)
    assert res["deferred"] == 1 and res["carry"] == [] and res["carried"] == 0
    assert len(_claims(db)) == 1
    assert db.artifacts("extract_llm", message_id=1) == []
    db.close()


def test_carried_result_with_changed_body_is_dropped(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    # the body was edited while the result waited for the lock: the claim
    # must lapse so the new body is selected, not be renewed
    db.db.execute("UPDATE messages SET content_hash='changed' WHERE message_id=1")
    db.db.commit()
    assert extract_llm._renew(db, carry[0][0], carry[0][1]) is None
    extract_llm._settle_carry(db, carry, final=False)
    assert carry == []
    assert db.artifacts("extract_llm", message_id=1) == []
    # our old claim is gone too, so the new body is selectable at once
    assert _claims(db) == []
    rows, _ = extract_llm._select_pending(db, 1, None, None, False)
    assert len(rows) == 1
    db.close()


def test_run_pending_drop_of_changed_body_releases_own_claim(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    db.db.execute("UPDATE messages SET content_hash='changed' WHERE message_id=1")
    db.db.commit()
    res = extract_llm.run_pending(db, limit=1, budget_s=30, per_write_lock=True,
                                  write_wait_s=1, carry=carry)
    assert res["carry"] == [] and res["deferred"] == 1
    assert _claims(db) == []
    db.close()


@pytest.mark.parametrize("settle", [False, True])
def test_drop_of_lost_lease_preserves_the_replacement_claim(tmp_path, monkeypatch,
                                                            settle):
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    db.db.execute("UPDATE fetch_jobs SET next_try=next_try+10 WHERE kind='extract_claim'")
    db.db.commit()
    replacement = _claims(db)
    if settle:
        extract_llm._settle_carry(db, carry, final=False)
    else:
        carry = extract_llm.run_pending(db, limit=1, budget_s=30, per_write_lock=True,
                                        write_wait_s=1, carry=carry)["carry"]
    assert carry == []
    assert _claims(db) == replacement
    db.close()


@pytest.mark.parametrize("settle", [False, True])
def test_transient_error_after_a_renewal_keeps_the_renewed_lease(tmp_path, monkeypatch,
                                                                 settle):
    """A sqlite error on the second carried item must not leave the
    first item's in-memory lease behind its renewed DB lease: the
    resident retries the same list and would otherwise drop a paid
    result as "not owned"."""
    import sqlite3
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=i, body="合成の本文です。" * 20) for i in (1, 2)])
    state = _lock_stub(monkeypatch, tmp_path, blocked_after_call=10 ** 6)
    carry = extract_llm.run_pending(db, limit=10, budget_s=30, per_write_lock=True,
                                    write_wait_s=1, carry=[])["carry"]
    assert len(carry) == 2
    original = extract_llm._renew
    calls = []

    def interrupted_renew(ledger, row, lease):
        calls.append(row["message_id"])
        if len(calls) == 2:
            raise sqlite3.OperationalError("database is locked")
        return original(ledger, row, lease)

    monkeypatch.setattr(extract_llm, "_renew", interrupted_renew)
    with pytest.raises(sqlite3.OperationalError):
        if settle:
            extract_llm._settle_carry(db, carry, final=False)
        else:
            extract_llm.run_pending(db, limit=10, budget_s=30, per_write_lock=True,
                                    write_wait_s=1, carry=carry)
    monkeypatch.setattr(extract_llm, "_renew", original)
    assert sorted(_claims(db)) == sorted(lease for _r, lease, _c in carry)
    state["blocked"] = 0
    extract_llm._settle_carry(db, carry, final=False)
    assert carry == []
    assert len(db.artifacts("extract_llm")) == 2
    assert _claims(db) == []
    db.close()


def test_non_final_settle_renews_kept_lease(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    _decay_lease(db, carry, extract_llm._CARRY_MARGIN_S + 80)
    extract_llm._settle_carry(db, carry, final=False)
    assert len(carry) == 1
    assert carry[0][1] - time.time() > 900 + extract_llm._CARRY_MARGIN_S
    assert _claims(db) == [carry[0][1]]
    db.db.execute("DELETE FROM fetch_jobs WHERE kind='extract_claim'")
    db.db.commit()
    extract_llm._settle_carry(db, carry, final=False)
    assert carry == []
    db.close()


@pytest.mark.parametrize("lock_free, artifacts", [(True, 1), (False, 0)])
def test_settle_carry_commits_or_releases(tmp_path, monkeypatch, lock_free,
                                          artifacts):
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    if lock_free:
        state["blocked"] = 0
    extract_llm._settle_carry(db, carry)
    assert carry == []
    assert len(db.artifacts("extract_llm", message_id=1)) == artifacts
    assert _claims(db) == []
    db.close()


def test_resident_threads_carry_and_settles_on_exit(monkeypatch, capsys):
    """--all --lane 2: run_pending gets the resident write wait, the
    previous call's carry rides into the next, _settle_carry runs at
    exit, and the lane's env lives only for the run."""
    now = [0.0]
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(extract_llm.time, "sleep",
                        lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    monkeypatch.setattr(extract_llm.sys, "argv", [
        "extract_llm", "--all", "--lane", "2", "--stop-after", "100"])
    monkeypatch.setattr(extract_llm, "Ledger",
                        lambda *a: type("DB", (), {"close": lambda self: None})())
    monkeypatch.delenv("MCS_LLM_SLOT", raising=False)
    monkeypatch.delenv(extract_llm.local_llm.TURN_LOCK_ENV, raising=False)
    seen, settled = [], []

    def run(*args, **kwargs):
        seen.append((kwargs["write_wait_s"], list(kwargs["carry"]),
                     os.environ.get("MCS_LLM_SLOT"),
                     os.environ.get(extract_llm.local_llm.TURN_LOCK_ENV)))
        now[0] += 60
        return {"done": 1, "failed": 0, "left": 1, "selected": 1,
                "carry": [("row", 0.0, "commit%d" % len(seen))]}

    monkeypatch.setattr(extract_llm, "run_pending", run)
    monkeypatch.setattr(extract_llm, "_settle_carry",
                        lambda led, carry, **kw: settled.append(list(carry)))
    assert extract_llm.main() == 0
    turn_lock = os.path.join(extract_llm.HOME, "data", "llm_turn.lock")
    wire = str(extract_llm.local_llm.LANES[2])
    assert [s[0] for s in seen] == [extract_llm._RESIDENT_WRITE_WAIT_S] * 2
    assert [s[1] for s in seen] == [[], [("row", 0.0, "commit1")]]
    assert all(s[2:] == (wire, turn_lock) for s in seen)
    assert settled == [[("row", 0.0, "commit2")]]
    assert "MCS_LLM_SLOT" not in os.environ
    assert extract_llm.local_llm.TURN_LOCK_ENV not in os.environ
    assert extract_llm._SLOT_OVERRIDE is None


@pytest.mark.parametrize("lane", ["1", "4"])
def test_unknown_lane_is_rejected(monkeypatch, capsys, lane):
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    monkeypatch.setattr(extract_llm.sys, "argv",
                        ["extract_llm", "--all", "--lane", lane])
    assert extract_llm.main() == 2
    assert '"bad_lane"' in capsys.readouterr().out
    assert "MCS_LLM_SLOT" not in os.environ


def test_old_slot_option_is_gone(monkeypatch):
    monkeypatch.setattr(extract_llm.sys, "argv",
                        ["extract_llm", "--all", "--slot", "0"])
    with pytest.raises(SystemExit):
        extract_llm.main()




def test_real_carry_is_not_serialized_or_crashes_resident(monkeypatch):
    """A real carry holds rows and commit thunks; the resident must log
    the pass without them instead of dying on json.dumps (Codex L1)."""
    now = [0.0]
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(extract_llm.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    monkeypatch.setattr(extract_llm.sys, "argv", ["extract_llm", "--all", "--lane", "2", "--stop-after", "10"])
    monkeypatch.setattr(extract_llm, "Ledger", lambda *a: type("DB", (), {"close": lambda self: None})())
    monkeypatch.delenv("MCS_LLM_SLOT", raising=False)
    monkeypatch.delenv(extract_llm.local_llm.TURN_LOCK_ENV, raising=False)
    settled = []

    def run(*args, **kwargs):
        now[0] += 20
        return {"done": 0, "failed": 0, "left": 1, "selected": 1,
                "carry": [({"body_text": "完全合成の投稿"}, 123.0, lambda: None)]}
    monkeypatch.setattr(extract_llm, "run_pending", run)
    monkeypatch.setattr(extract_llm, "_settle_carry",
                        lambda led, carry, **kw: settled.append(len(carry)))
    assert extract_llm.main() == 0
    assert settled == [1]


def test_saved_carried_result_counts_as_current_progress(tmp_path, monkeypatch):
    """The pass that finally writes a carried result reports it as its
    own progress (done/pids), not only as carried (Codex L2)."""
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    state["blocked"] = 0
    result = extract_llm.run_pending(db, limit=10, budget_s=30, per_write_lock=True,
                                     write_wait_s=1, carry=carry)
    assert len(db.artifacts("extract_llm", message_id=1)) == 1
    assert result["done"] == 1 and result["pids"] == [1] and result["carried"] == 1
    db.close()


def test_context_lane_normal_settlement_preserves_blocked_result(tmp_path, monkeypatch):
    """Lane 3's pre-context settlement keeps a still lock-bound result
    carried (and its claim) instead of discarding it (Codex L3)."""
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    extract_llm._settle_carry(db, carry, final=False)
    assert carry, "ordinary lane-3 iteration discarded the completed result"
    assert _claims(db) != []
    assert db.artifacts("extract_llm", message_id=1) == []
    state["blocked"] = 0
    extract_llm._settle_carry(db, carry, final=False)
    assert carry == [] and _claims(db) == []
    assert len(db.artifacts("extract_llm", message_id=1)) == 1
    db.close()


def test_update_stop_does_not_commit_old_carried_result(tmp_path, monkeypatch):
    """An updater fence seen mid-pass exits without writing the carried
    result under old code; the claim is released for the new resident."""
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    state["blocked"] = 0
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(extract_llm, "Ledger", lambda *a: db)
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 123))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 123))
    monkeypatch.setattr(extract_llm.sys, "argv", ["extract_llm", "--all", "--lane", "2", "--semantic", "--stop-after", "10"])
    reached = []

    def run(*args, **kwargs):
        reached.append("run_pending")
        return {"done": 0, "failed": 0, "left": 1, "selected": 1, "carry": carry}

    monkeypatch.setattr(extract_llm, "run_pending", run)

    def updated(*args):
        reached.append("updated")
        monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 124))
        return {"run_lock_lost": "updated"}
    monkeypatch.setattr(extract_llm, "_background_semantic", updated)
    assert extract_llm.main() == 0
    assert reached == ["run_pending", "updated"]
    assert db.artifacts("extract_llm", message_id=1) == []
    assert _claims(db) == []
    db.db.close()


def test_context_lane_checks_update_fence_before_settling_carry(tmp_path, monkeypatch):
    """Lane 3 compares the updater fence before its pre-context settle, so
    an update between passes never lets old code write the carried
    result (Codex L4 follow-up)."""
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    state["blocked"] = 0
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(extract_llm, "Ledger", lambda *a: db)
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 123))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 123))
    monkeypatch.setattr(extract_llm.sys, "argv", ["extract_llm", "--all", "--lane", "3", "--stop-after", "10"])

    reached = []

    def run(*a, **kw):
        reached.append("run_pending")
        monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 124))
        return {"done": 0, "failed": 0, "left": 1, "selected": 1, "carry": carry}
    monkeypatch.setattr(extract_llm, "run_pending", run)
    monkeypatch.setattr(extract_llm, "_background_context_refresh",
                        lambda *a: {"status": "deferred", "done": 0})
    assert extract_llm.main() == 0
    assert reached == ["run_pending"]
    assert db.artifacts("extract_llm", message_id=1) == []
    assert _claims(db) == []
    db.db.close()


def test_lane3_rechecks_update_after_acquiring_settlement_lock(tmp_path, monkeypatch):
    """An updater that completes while the pre-context settlement waits
    for run.lock is seen under the lock: the carried result is released,
    never written by old code (Codex cc61f4b6 race)."""
    db = _ledger(tmp_path)
    _state, carry = _carry_one(db, monkeypatch, tmp_path)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(extract_llm, "Ledger", lambda *args: db)
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    generation = [123]
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, generation[0]))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 123))
    monkeypatch.setattr(extract_llm.sys, "argv", [
        "extract_llm", "--all", "--lane", "3", "--stop-after", "10"])
    armed = [False]
    lock_attempts = []

    def acquire():
        if armed[0] and generation[0] == 123:
            # the pre-context fence check just passed; the updater then
            # holds run.lock, bumps the stamp and releases it before
            # _write_lock's next attempt
            lock_attempts.append("updater_completes_during_wait")
            generation[0] = 124
            return None
        lock_attempts.append("acquired_after_update")
        return os.open(tmp_path / "settle.lock", os.O_CREAT | os.O_RDWR)
    monkeypatch.setattr(extract_llm, "acquire_run_lock", acquire)

    def run(*args, **kwargs):
        armed[0] = True
        return {"done": 0, "failed": 0, "left": 1, "selected": 1, "carry": carry}
    monkeypatch.setattr(extract_llm, "run_pending", run)

    def context_refresh(ledger, initial, stop):
        return {"status": "off", "done": 0, **(
            {"run_lock_lost": "updated"}
            if writer_lock.updater_state() != initial else {})}
    monkeypatch.setattr(extract_llm, "_background_context_refresh", context_refresh)
    try:
        assert extract_llm.main() == 0
        assert lock_attempts[:2] == [
            "updater_completes_during_wait", "acquired_after_update"]
        assert db.artifacts("extract_llm", message_id=1) == []
        assert _claims(db) == []
    finally:
        db.db.close()


def test_failure_write_waits_for_lock_instead_of_skipping(tmp_path, monkeypatch):
    """A failed extraction's error row is not carried, so it must wait out
    the tick's lock (default 60 s) rather than the resident's 3 s: a
    skipped _fail re-selects the row for another paid LLM call."""
    db = _ledger(tmp_path)
    db.save_messages([_message(body="合成の本文です。" * 20)])
    state = _lock_stub(monkeypatch, tmp_path, blocked_after_call=0)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)

    def extract(body, **_kw):
        state["blocked"] = 10   # longer than the resident wait, shorter than 60
        return None
    monkeypatch.setattr(extract_llm, "llm_extract", extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, per_write_lock=True,
                                  write_wait_s=extract_llm._RESIDENT_WRITE_WAIT_S,
                                  carry=[])
    assert res["failed"] == 1
    metas = [json.loads(a["meta"]) for a in db.artifacts("extract_llm", message_id=1)]
    assert [m["attempts"] for m in metas if m.get("error")] == [1]
    assert _claims(db) == []
    db.close()


def test_lane3_survives_transient_settle_error(monkeypatch, capsys):
    """A sqlite error from the pre-context settle is logged as
    extract_loop_error and the resident keeps running."""
    import sqlite3
    now = [0.0]
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(extract_llm.time, "sleep",
                        lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 123))
    monkeypatch.setattr(extract_llm.sys, "argv", [
        "extract_llm", "--all", "--lane", "3", "--stop-after", "100"])
    monkeypatch.setattr(extract_llm, "Ledger",
                        lambda *a: type("DB", (), {"close": lambda self: None})())
    monkeypatch.setattr(extract_llm, "_background_context_refresh",
                        lambda *a: {"status": "off", "done": 0})
    settles = []

    def settle(led, carry, **kw):
        settles.append(kw)
        if len(settles) == 1:
            raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(extract_llm, "_settle_carry", settle)
    monkeypatch.setattr(extract_llm, "run_pending", lambda *a, **kw: {
        "done": 1, "failed": 0, "left": 1, "selected": 1, "carry": []})
    assert extract_llm.main() == 0
    assert "extract_loop_error" in capsys.readouterr().err
    assert len(settles) >= 3   # raised, retried, exit settle


def test_lane_three_settlement_reports_saved_result(tmp_path, monkeypatch, capsys):
    db = _ledger(tmp_path)
    try:
        state, carry = _carry_one(db, monkeypatch, tmp_path)
        state["blocked"] = 0
        extract_llm._settle_carry(db, carry, final=False)
        rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert rows[-1]["carry_settled"] == {"done": 1, "held_prior": 0, "deferred": 0}
        assert len(db.artifacts("extract_llm", message_id=1)) == 1
    finally:
        db.close()


@pytest.mark.parametrize("lane_three", [False, True])
def test_carried_safety_hold_is_reported_in_the_settling_pass(tmp_path, monkeypatch, capsys, lane_three):
    db = _ledger(tmp_path)
    try:
        body = "本人に咳はありません。" + "合成の経過報告です。" * 20
        db.save_messages([_message(body=body)])
        row = db.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()
        prior = db.artifact_add("extract_llm", json.dumps({"symptoms": [{
            "text": "咳", "negated": True, "subject": "patient",
            "evidence": "本人に咳はありません。"}]}), project_id=1, message_id=1,
            meta={"hash": row[0], "extract_version": extract_llm.EXTRACT_VERSION - 1})
        state = _lock_stub(monkeypatch, tmp_path, blocked_after_call=10 ** 6)
        first = extract_llm.run_pending(db, limit=1, budget_s=30,
            per_write_lock=True, write_wait_s=1, carry=[])
        carry = first["carry"]
        assert len(carry) == 1
        state["blocked"] = 0
        if lane_three:
            extract_llm._settle_carry(db, carry, final=False)
            rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
            result = rows[-1]["carry_settled"]
        else:
            result = extract_llm.run_pending(db, limit=1, budget_s=30,
                per_write_lock=True, write_wait_s=1, carry=carry)
        assert result["done"] == 0 and result["held_prior"] == 1
        assert [a["artifact_id"] for a in db.artifacts("extract_llm", message_id=1)] == [prior]
        assert _claims(db) == []
    finally:
        db.close()
