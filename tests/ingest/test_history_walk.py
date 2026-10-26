"""Bounded page evidence keeps overlaps, ordering and scope explicit."""
import copy
from types import SimpleNamespace

import pytest

import history_walk as walk

COHORT = "a"*32


def _batch(start, pages, total=5, ids=None):
    obs = [walk.observation(1,0,p,10,p<total,ids if ids is not None else [p])
           for p in range(start,min(start+pages,total+1))]
    return SimpleNamespace(pages=len(obs),reached=obs[-1]["has_next"] is False,
                           error=None,page_observations=obs)


def _scan(budget):
    payload = {"since":0,"page":1,"walk":walk.start(1,COHORT)}
    calls = []
    for _ in range(20):
        cursor = payload["page"]
        start = cursor if budget == 1 else max(1,cursor-1)
        calls.append(start)
        result = walk.advance(payload,_batch(start,budget),1,COHORT,start,budget,checkpoint_safe=True)
        if result == "accepted":
            return payload,calls
    pytest.fail("stable walk did not converge")


def test_one_and_three_page_budgets_fold_the_same_pages_once():
    one, a = _scan(1)
    three, b = _scan(3)
    assert a == [1,2,3,4,5]*2 and b == [1,3]*2
    assert one["walk"]["digest"] == three["walk"]["digest"]
    assert walk.accepted(one,1,COHORT)
    assert len(str(one)) < 700


@pytest.mark.parametrize("field,value", [("v",True),("scope","b"*64),("digest",None),
                                          ("last","c"*64),("mismatches",True),("phase","done")])
def test_corrupt_walk_is_not_a_legacy_payload(field,value):
    payload = {"since":0,"page":1,"walk":walk.start(1,COHORT)}
    payload["walk"][field] = value
    with pytest.raises(ValueError):
        walk.validate(payload,1,COHORT)


@pytest.mark.parametrize("field,value", [("page",2),("project_id",2),("since",1),
                                          ("v",2),("per_page",20),("has_next",0),("digest","bad")])
def test_observation_scope_order_and_shape_are_checked(field,value):
    payload = {"since":0,"page":1,"walk":walk.start(1,COHORT)}
    batch = _batch(1,1)
    batch.page_observations[0][field] = value
    with pytest.raises(ValueError):
        walk.advance(payload,batch,1,COHORT,1,1,checkpoint_safe=True)


def test_overlap_change_discards_prior_and_keeps_monotonic_mismatch_count():
    payload,_ = _scan(3)
    payload["walk"] = walk.start(1,COHORT,prior=payload["walk"]["digest"])
    payload["page"] = 1
    walk.advance(payload,_batch(1,3),1,COHORT,1,3,checkpoint_safe=True)
    result = walk.advance(payload,_batch(3,3,ids=[99]),1,COHORT,3,3,checkpoint_safe=True)
    assert result == "mismatch" and payload["page"] == 1
    assert payload["walk"]["prior"] is None and payload["walk"]["mismatches"] == 1


def test_unsafe_checkpoint_cannot_move_evidence_and_partial_pages_do_not_finish():
    payload = {"since":0,"page":1,"walk":walk.start(1,COHORT)}
    before = copy.deepcopy(payload)
    assert walk.advance(payload,_batch(1,3),1,COHORT,1,3,checkpoint_safe=False) == "blocked"
    assert payload == before
    batch = _batch(1,2)
    batch.error = RuntimeError("synthetic")
    assert walk.advance(payload,batch,1,COHORT,1,3,checkpoint_safe=True) == "progress"
    assert payload["page"] == 3 and payload["walk"]["prior"] is None
