"""Extraction retries a result whose per-write lock wait timed out,
carries it across resident run_pending calls, and the --lane CLI maps
lanes to wire slots."""
import os

import pytest

import extract_llm
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
    return db.db.execute(
        "SELECT next_try FROM fetch_jobs WHERE kind='extract_claim'").fetchall()


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
                        lambda led, carry: settled.append(list(carry)))
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
    monkeypatch.setattr(extract_llm, "_settle_carry", lambda led, carry: settled.append(len(carry)))
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
    import mcs_util
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    state["blocked"] = 0
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(extract_llm, "Ledger", lambda *a: db)
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(mcs_util, "_updater_state", lambda: (False, 123))
    monkeypatch.setattr(extract_llm.sys, "argv", ["extract_llm", "--all", "--lane", "2", "--semantic", "--stop-after", "10"])
    monkeypatch.setattr(extract_llm, "run_pending", lambda *a, **kw: {
        "done": 0, "failed": 0, "left": 1, "selected": 1, "carry": carry})

    def updated(*args):
        monkeypatch.setattr(mcs_util, "_updater_state", lambda: (False, 124))
        return {"run_lock_lost": "updated"}
    monkeypatch.setattr(extract_llm, "_background_semantic", updated)
    assert extract_llm.main() == 0
    assert db.artifacts("extract_llm", message_id=1) == []
    assert _claims(db) == []
    db.db.close()


def test_context_lane_checks_update_fence_before_settling_carry(tmp_path, monkeypatch):
    """Lane 3 compares the updater fence before its pre-context settle, so
    an update between passes never lets old code write the carried
    result (Codex L4 follow-up)."""
    import mcs_util
    db = _ledger(tmp_path)
    state, carry = _carry_one(db, monkeypatch, tmp_path)
    state["blocked"] = 0
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(extract_llm, "Ledger", lambda *a: db)
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(mcs_util, "_updater_state", lambda: (False, 123))
    monkeypatch.setattr(extract_llm.sys, "argv", ["extract_llm", "--all", "--lane", "3", "--stop-after", "10"])

    def run(*a, **kw):
        monkeypatch.setattr(mcs_util, "_updater_state", lambda: (False, 124))
        return {"done": 0, "failed": 0, "left": 1, "selected": 1, "carry": carry}
    monkeypatch.setattr(extract_llm, "run_pending", run)
    monkeypatch.setattr(extract_llm, "_background_nightly",
                        lambda *a: {"status": "deferred", "done": 0})
    assert extract_llm.main() == 0
    assert db.artifacts("extract_llm", message_id=1) == []
    assert _claims(db) == []
    db.db.close()
