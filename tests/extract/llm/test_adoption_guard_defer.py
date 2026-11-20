"""Deferred adoption guard: off-transaction evaluation fenced by cloud artifacts."""
import extract_llm as llm
import extract_llm_runner as runner
from inference_v1_writers_testkit import source as source


def test_cloud_guard_fence_tracks_only_cloud_artifact_kinds(source):
    led, row = source
    before = runner._cloud_guard_fence(led.db)
    led.artifact_add(llm.KIND, '{"summary":"合成"}', project_id=row["project_id"],
                     message_id=row["message_id"])
    assert runner._cloud_guard_fence(led.db) == before
    led.artifact_add("cloud_usage_reserve", "{}", project_id=row["project_id"],
                     message_id=row["message_id"])
    after = runner._cloud_guard_fence(led.db)
    assert after != before
    assert after["cloud_usage_reserve"][0] == 1


def test_deferred_guard_reuses_verdict_while_fence_holds():
    state = {"evals": 0, "fence": {"cloud_usage_reserve": (1, 10)}}

    def evaluate():
        state["evals"] += 1
        return True

    guard = runner._DeferredGuard(evaluate, lambda: dict(state["fence"]))
    guard.prepare()
    assert guard() is True and guard() is True
    assert state["evals"] == 1


def test_deferred_guard_reevals_when_fence_breaks():
    state = {"evals": 0, "fence": {"cloud_usage_reserve": (1, 10)}}

    def evaluate():
        state["evals"] += 1
        return state["evals"] < 2

    guard = runner._DeferredGuard(evaluate, lambda: dict(state["fence"]))
    guard.prepare()
    state["fence"] = {"cloud_usage_reserve": (2, 11)}
    assert guard() is False
    assert state["evals"] == 2


def test_deferred_guard_fence_predates_evaluation():
    """A cloud write landing mid-evaluation must break the commit-time fence:
    the guard's pinned snapshot cannot see it, so a post-evaluation fence
    would wrongly accept the stale verdict."""
    fence_state = {"k": (1, 1)}
    evals = []

    def evaluate():
        evals.append(1)
        fence_state["k"] = (2, 2)   # cloud artifact commit during evaluation
        return True

    guard = runner._DeferredGuard(evaluate, lambda: dict(fence_state))
    guard.prepare()
    assert guard() is True
    assert len(evals) == 2   # fence mismatch forced the in-transaction re-eval


def test_deferred_guard_reevals_false_verdict_without_fence():
    state = {"evals": 0}

    def evaluate():
        state["evals"] += 1
        return state["evals"] > 1

    guard = runner._DeferredGuard(evaluate, lambda: {"f": (1, 1)})
    guard.prepare()
    assert guard() is True
    assert state["evals"] == 2


def test_deferred_guard_falls_back_when_prepare_fails():
    state = {"evals": 0}

    def evaluate():
        state["evals"] += 1
        if state["evals"] == 1:
            raise RuntimeError("synthetic prepare failure")
        return True

    guard = runner._DeferredGuard(evaluate, lambda: {"f": (1, 1)})
    guard.prepare()
    assert guard() is True
    assert state["evals"] == 2


def test_deferred_guard_prepare_runs_outside_write_transaction(source):
    led, row = source
    seen = []

    class probe:
        def prepare(self):
            seen.append(("prepare", led.db.in_transaction))

        def __call__(self):
            seen.append(("call", led.db.in_transaction))
            return True

    assert llm._claim(led, row) is not None
    llm._commit_extraction_atomic(
        led, row, '{"summary":"合成候補"}', None, None, None, None, (0,),
        check_current=False, adoption_guard=probe())
    assert seen[0] == ("prepare", False)
    assert seen[1:] and all(txn for _tag, txn in seen[1:])
