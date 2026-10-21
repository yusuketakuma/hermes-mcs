"""Re-extraction order: update-released automatic holds run last.

A code update changes the hold contract, so every held body re-enters
pending; those re-runs mostly end held again. Selection keeps them in the
same class, only after the other re-extractions. Synthetic temp DB only."""
import json

import pytest

import extract_llm
from extract_llm import EXTRACT_VERSION, KIND, UPGRADE_KIND
from extract_testkit import _hash, _ledger, _message


def _corpus(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=mid, body=f"体温は37度です。経過観察 {mid}",
                               posted_at=f"2026-09-0{mid}T00:00:00+09:00")
                      for mid in (1, 2, 3)])
    priors = {mid: db.artifact_add(      # previous generation: all pending
        KIND, json.dumps({"summary": "s"}), project_id=1, message_id=mid,
        meta={"hash": _hash(db, mid), "extract_version": EXTRACT_VERSION - 1})
        for mid in (1, 2, 3)}
    return db, priors


def _hold(db, mid, prior, *, reason="protected_loss", invalidated=False, contract="older-code"):
    db.artifact_add(UPGRADE_KIND, json.dumps({
        "state": "held_prior", "reason": reason, "hash": _hash(db, mid),
        "prior_artifact_id": prior, "target_version": EXTRACT_VERSION, "contract": contract}),
        project_id=1, message_id=mid, meta={"invalidated": 1} if invalidated else {})


def _order(db, limit=3):
    rows, _ = extract_llm._select_pending(db, limit, None, None, False)
    return [r["message_id"] for r in rows]


def test_an_update_released_hold_runs_after_the_other_reextractions(tmp_path):
    db, priors = _corpus(tmp_path)
    assert _order(db) == [3, 2, 1]                 # newest first, as before
    _hold(db, 3, priors[3])
    assert _order(db) == [2, 1, 3]                 # still pending, just last
    assert _order(db, limit=1) == [2]


def test_a_current_contract_hold_stays_out_of_the_backlog(tmp_path):
    db, priors = _corpus(tmp_path)
    _hold(db, 3, priors[3], contract=extract_llm._upgrade_contract())
    assert _order(db) == [2, 1]


@pytest.mark.parametrize("hold", [{"invalidated": True}, {"reason": "human_failed"},
                                  {"prior": "other"}])
def test_stale_invalidated_and_human_holds_are_not_pushed_back(tmp_path, hold):
    db, priors = _corpus(tmp_path)
    # a receipt for another prior extract no longer describes this body's state
    prior = priors[1] if hold.pop("prior", None) else priors[3]
    _hold(db, 3, prior, **hold)
    assert _order(db) == [3, 2, 1]
