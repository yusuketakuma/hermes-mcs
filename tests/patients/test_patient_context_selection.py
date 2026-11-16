"""Synthetic independent invariants for summary-side context selection."""
import copy
import pytest
import patient_context_selection as selection


def fact(identity, category, text, stamp):
    return {"id": identity, "category": category, "text": text,
            "source": "chat", "project_id": 1, "message_id": 1,
            "posted_at": None, "posted_at_ts": stamp, "subject": "unspecified"}


def test_category_fairness_and_summary_source_identity(monkeypatch):
    values = [fact("a", "living", "家族は過去の居住を希望していません。", 3),
              fact("b", "living", "本人の転居予定は未確認。", 2),
              fact("c", "allergies", "アレルギーは未確認。", 1)]
    original = copy.deepcopy(values)
    monkeypatch.setattr(selection, "MAX_FACTS", 2)
    selected, total = selection.select_facts({f["id"]: f for f in values})
    assert [f["id"] for f in selected] == ["c", "a"]
    assert total == 3
    reports = selection.summary_reports(values + [values[0]])
    assert [r["id"] for r in reports] == ["a", "b", "c"]
    assert selection.summary_selection({"fact_ids": ["b", "a"]}, reports) == [
        {"text": "本人の転居予定は未確認。", "fact_ids": ["b"]},
        {"text": "家族は過去の居住を希望していません。", "fact_ids": ["a"]}]
    assert values == original


@pytest.mark.parametrize("answer", [{"fact_ids": []}, {"fact_ids": ["a", "a"]},
    {"fact_ids": ["unknown"]}, {"fact_ids": [1]}, {"fact_ids": ["a"], "text": "推論"}])
def test_summary_rejects_unbound_or_generated_content(answer):
    reports = selection.summary_reports([fact("a", "living", "原文は未確認。", 0)])
    with pytest.raises(ValueError, match="patient_context_unsupported_summary"):
        selection.summary_selection(answer, reports)



BASE = 1_800_000_000  # synthetic posting time (2027-01-15 JST); every fact below is invented
PREFIX = {"current": "c", "conflict": "k", "plan": "p", "unknown": "u", "history": "h"}


def bucketed(identity, bucket, index=0, category="living", **extra):
    return {**fact(identity, category, "合成の記録。", BASE - index), "view_bucket": bucket, **extra}


def ids(selected):
    return [f["id"] for f in selected]


def pool(**counts):
    # Within a bucket, index 0 is the newest, so ids sort newest first.
    values = [bucketed(f"{PREFIX[bucket]}{i:02d}", bucket, i)
              for bucket, count in counts.items() for i in range(count)]
    return {f["id"]: f for f in values}


def test_full_buckets_follow_twelve_slot_weights():
    selected, total = selection.select_facts(pool(current=30, conflict=30, plan=30, unknown=30, history=30))
    assert total == 150 and len(selected) == 150
    prefix = [f["view_bucket"] for f in selected[:24]]
    assert {b: prefix.count(b) for b in PREFIX} == {
        "current": 8, "conflict": 6, "plan": 4, "unknown": 4, "history": 2}
    for n in range(1, len(selected) + 1):
        head = selected[:n]
        if sum(f["view_bucket"] != "history" for f in head) < 120:
            assert sum(f["view_bucket"] == "history" for f in head) <= -(-n // 12)


def test_history_continues_after_current_runs_out():
    selected, total = selection.select_facts(pool(current=5, history=50))
    assert total == 55
    assert ids(selected) == [f"c{i:02d}" for i in range(5)] + [f"h{i:02d}" for i in range(50)]


@pytest.mark.parametrize("counts, expected", [
    # Empty current: each slot borrows current, conflict, plan, unknown in that order.
    ({"conflict": 1, "plan": 2, "unknown": 2, "history": 2},
     ["k00", "p00", "p01", "u00", "u01", "h00", "h01"]),
    # Empty history: the 12th (history) slot takes from current first.
    ({"current": 10, "conflict": 3},
     ["c00", "k00", "c01", "c02", "c03", "k01", "c04", "c05", "c06", "k02", "c07", "c08", "c09"]),
    ({"history": 3}, ["h00", "h01", "h02"]),
    ({"plan": 3}, ["p00", "p01", "p02"]),
    ({"current": 1, "plan": 1, "history": 3}, ["c00", "p00", "h00", "h01", "h02"]),
])
def test_empty_slots_fall_back_in_fixed_order(counts, expected):
    selected, total = selection.select_facts(pool(**counts))
    assert ids(selected) == expected
    assert total == sum(counts.values())


def test_selection_is_capped_and_excludes_unknown_buckets_and_categories():
    values = pool(current=80, conflict=60, plan=60, unknown=60, history=40)
    stray = {"x_bucket": bucketed("x_bucket", "elsewhere"),
             "x_category": bucketed("x_category", "current", category="not_a_category")}
    selected, total = selection.select_facts({**values, **stray})
    assert total == 300 and len(selected) == selection.MAX_FACTS == 256
    assert len(set(ids(selected))) == 256 and set(ids(selected)) <= set(values)
    small = pool(current=3, conflict=2, plan=1, unknown=4, history=5)
    everything, count = selection.select_facts({**small, **stray})
    # A duplicate-free permutation of the five buckets' facts: nothing lost, nothing resurrected.
    assert sorted(ids(everything)) == sorted(small) and count == len(everything) == 15


def test_recency_uses_state_day_then_posted_day_then_timestamp():
    # Same category and bucket, so only recency decides the order.
    newer_state = bucketed("a", "current", 9, temporal={"state_day": "2026-10-10", "posted_day": "2026-10-01"})
    newer_post = bucketed("b", "current", 0, temporal={"state_day": "2026-10-05", "posted_day": "2026-10-20"})
    assert ids(selection.select_facts({"a": newer_state, "b": newer_post})[0]) == ["a", "b"]
    # Without state_day, posted_day decides even against a newer timestamp.
    older_day = bucketed("a", "current", 0, temporal={"state_day": None, "posted_day": "2026-10-01"})
    newer_day = bucketed("b", "current", 9, temporal={"posted_day": "2026-10-02"})
    assert ids(selection.select_facts({"a": older_day, "b": newer_day})[0]) == ["b", "a"]
    # Without temporal, the JST day of a finite posted_at_ts stands in.
    stamp = 1_792_000_000  # 2026-10-15 JST
    bare = {**fact("x", "living", "合成。", stamp), "view_bucket": "current"}
    dated = {**fact("y", "living", "合成。", stamp + 10**6), "view_bucket": "current",
             "temporal": {"state_day": "2026-10-01"}}
    assert ids(selection.select_facts({"x": bare, "y": dated})[0]) == ["x", "y"]
    # Equal day and time keep the previous tie-break: id descending (reverse=True).
    tie = {k: bucketed(k, "current", 0) for k in ("a", "b")}
    assert ids(selection.select_facts(tie)[0]) == ["b", "a"]


@pytest.mark.parametrize("temporal", [None, "2026-10-01", {"state_day": 20261018}, {"state_day": "20261018"},
                                      {"state_day": "2026-W42-1"}, {"state_day": "2026-02-30"},
                                      {"posted_day": None}, {"state_day": b"2026-10-01"}])
@pytest.mark.parametrize("stamp", [None, True, float("nan"), float("inf"), "1800000000", 10**400])
def test_unreadable_recency_values_sort_last_without_raising(temporal, stamp):
    odd = {**fact("odd", "living", "合成。", stamp), "view_bucket": "current", "temporal": temporal}
    assert selection._recency(odd) == (0, 0.0, "odd")
    values = {"odd": odd, "good": bucketed("good", "current", 0, temporal={"posted_day": "2026-10-01"}),
              "plain": bucketed("plain", "current", 1), "none": bucketed("none", "current", 2, posted_at_ts=None)}
    first = ids(selection.select_facts(values)[0])
    assert first == ["plain", "good", "odd", "none"]
    assert first == ids(selection.select_facts(dict(reversed(list(values.items()))))[0])


def test_unconvertible_finite_timestamp_keeps_fixed_key_types():
    # A finite time outside the platform's date range keeps its value but no day.
    key = selection._recency({**fact("far", "living", "合成。", -1e18), "view_bucket": "current"})
    assert key == (0, -1e18, "far") and [type(part) for part in key] == [int, float, str]
